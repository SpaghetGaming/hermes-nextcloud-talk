"""v1.19.0 adapter: dangerous-command (exec) approvals become reaction cards.

The runner calls ``_send_exec_approval_prompt`` (which flips
``supports_exec_approval_buttons`` on); we post the shared card text plus a
legend, then seed 👍/👎 reactions on our own message. A person's thumbs
reaction resolves the wait through ``tools.approval.resolve_gateway_approval_card``
— the same authority-checked path the Discord/Telegram buttons use — instead of
forcing them to type /approve.

No network: httpx MockTransport fakes Talk. The Hermes approval core is faked in
sys.modules (``tools.approval`` / ``gateway.approval_actor``) with the real call
shapes; under the tests' gateway stub the real base may lack the exec-approval
surface, so those probes are hasattr-guarded.
"""

import asyncio
import json as _j
import re
import sys
import types

import httpx

from user.adapter import EXEC_APPROVAL_EMOJIS, NextcloudTalkUserAdapter, ROOM_TYPE_ONE_TO_ONE
from gateway.config import PlatformConfig

THUMB_UP, THUMB_DOWN = "\U0001F44D", "\U0001F44E"


class _Prompt:
    """Stand-in for gateway.platforms.base.ExecApprovalPrompt."""

    _seq = 0

    def __init__(self, chat_id="dmx1", session_key="sk-1", request_id=None,
                 choices=("once", "session", "always", "deny")):
        type(self)._seq += 1
        self.chat_id = chat_id
        self.session_key = session_key
        self.request_id = request_id or f"rq-{type(self)._seq}"
        self.text = "⚠️ Dangerous command\n```rm -rf /tmp/x```"
        self.command = "rm -rf /tmp/x"
        self.description = "delete in root path"
        self.smart_denied = False
        self.metadata = None
        self.actions = [(c.upper(), c, "") for c in choices]

    @property
    def choices(self):
        return [c for _, c, _ in self.actions]


class Talk:
    """Talk REST subset: chat POST (incrementing ids), reaction POST/DELETE."""

    def __init__(self):
        self.posts = []        # chat messages: parsed JSON bodies
        self.reacted = {}      # message id -> [emojis added]
        self.unreacted = []    # (message id, emoji) removals
        self._next = 7000

    def __call__(self, request):
        path, method = request.url.path, request.method
        body = _j.loads(request.content) if request.content else {}
        if path.endswith("/chat/dmx1") and method == "POST":
            self._next += 1
            self.posts.append(body)
            return httpx.Response(201, json={"ocs": {"data": {"id": self._next}}})
        m = re.search(r"/reaction/dmx1/(\d+)$", path)
        if m:
            if method == "POST":
                self.reacted.setdefault(m.group(1), []).append(body["reaction"])
                return httpx.Response(200, json={"ocs": {"data": {}}})
            if method == "DELETE":
                self.unreacted.append((m.group(1), body["reaction"]))
                return httpx.Response(200, json={"ocs": {"data": {}}})
        return httpx.Response(200, json={"ocs": {"data": {}}})


def _adapter():
    a = NextcloudTalkUserAdapter(PlatformConfig(extra={
        "server_url": "https://cloud.example.com", "username": "hermes-bot",
        "app_password": "filler-not-secret", "allowed_users": "chef"}))
    a._threads_enabled = True
    a._capabilities_checked = True
    a._room_meta["dmx1"] = {"token": "dmx1", "type": ROOM_TYPE_ONE_TO_ONE,
                            "displayName": "chef"}
    return a


def _wire(a, talk):
    a._http_client = httpx.AsyncClient(base_url="https://cloud.example.com",
                                       transport=httpx.MockTransport(talk))


class ApprovalCore:
    """Fakes tools.approval + gateway.approval_actor with the real call shapes."""

    def __init__(self, monkeypatch, resolves=1, refusal=None):
        self.resolves = resolves          # resolve_gateway_approval_card's return
        self.refusal = refusal            # approval_refusal's return (None = allowed)
        self.resolved = []                # (session_key, choice, request_id, actor_id)
        self.observers = {}               # request_id -> settle callback
        tools_pkg = types.ModuleType("tools")
        approval = types.ModuleType("tools.approval")

        def resolve_gateway_approval_card(session_key, choice, request_id, *, actor_id=None,
                                          admin_ids=(), extra=None, admitted=True):
            self.resolved.append((session_key, choice, request_id, actor_id))
            return self.resolves

        def observe_gateway_settle(session_key, request_id, observer):
            self.observers[request_id] = observer
            return True

        approval.resolve_gateway_approval_card = resolve_gateway_approval_card
        approval.observe_gateway_settle = observe_gateway_settle
        approval.restrict_gateway_approval_card_to_deny = lambda *a, **k: None
        tools_pkg.approval = approval
        monkeypatch.setitem(sys.modules, "tools", tools_pkg)
        monkeypatch.setitem(sys.modules, "tools.approval", approval)

        actor = types.ModuleType("gateway.approval_actor")

        def approval_refusal(session_key, actor_id, *, admin_ids=(), request_id=None,
                             extra=None, choice=None, admitted=True):
            return self.refusal

        def refusal_text(requester):
            return f"Only {requester} can answer this approval."

        actor.approval_refusal = approval_refusal
        actor.refusal_text = refusal_text
        monkeypatch.setitem(sys.modules, "gateway.approval_actor", actor)
        import gateway
        monkeypatch.setattr(gateway, "approval_actor", actor, raising=False)


def _react(a, emoji, parent_id, *, actor="chef", mid=None):
    msg = {"id": mid or 8000, "actorId": actor, "actorType": "users",
           "actorDisplayName": actor.title(), "message": emoji, "messageType": "system",
           "systemMessage": "reaction", "messageParameters": {}, "timestamp": 1700000000,
           "parent": {"id": parent_id, "actorId": "hermes-bot", "actorType": "users",
                      "message": "card", "messageParameters": {}}}
    asyncio.run(a._on_message("dmx1", msg))


def _armed(monkeypatch, prompt=None):
    """Send one card through the real code path; returns (adapter, talk, core, card_id)."""
    a, talk, core = _adapter(), Talk(), ApprovalCore(monkeypatch)
    _wire(a, talk)
    res = asyncio.run(a._send_exec_approval_prompt(prompt or _Prompt()))
    assert res.success and res.message_id
    return a, talk, core, str(res.message_id)


# -- the card itself ---------------------------------------------------------------

def test_card_posts_legend_and_seeds_thumbs(monkeypatch):
    a, talk, core, card = _armed(monkeypatch)
    text = talk.posts[0]["message"]
    assert text.startswith("⚠️ Dangerous command")
    assert f"React {THUMB_UP} to approve" in text and f"{THUMB_DOWN} to deny" in text
    assert "`/approve session`" in text and "`/approve always`" in text
    assert talk.reacted[card] == [THUMB_UP, THUMB_DOWN]
    assert f"exec:dmx1:{card}" in a._exec_cards
    assert core.observers  # settle observer armed for the card's request


def test_supports_exec_approval_buttons_probe():
    try:
        from gateway.platforms.base import BasePlatformAdapter
    except Exception:
        return
    if not hasattr(BasePlatformAdapter, "supports_exec_approval_buttons"):
        return  # gateway stub without the exec-approval surface
    assert NextcloudTalkUserAdapter.supports_exec_approval_buttons()


def test_deny_only_legend():
    legend = NextcloudTalkUserAdapter._exec_approval_legend(["deny"])
    assert THUMB_UP not in legend and f"{THUMB_DOWN} to deny" in legend


# -- reactions answer the card ------------------------------------------------------

def test_thumbs_up_resolves_once(monkeypatch):
    a, talk, core, card = _armed(monkeypatch)
    _react(a, THUMB_UP, card)
    assert core.resolved == [("sk-1", "once", "rq-1", "chef")] or \
        [r[:2] + r[3:] for r in core.resolved] == [("sk-1", "once", "chef")]
    assert core.resolved[0][1] == "once" and core.resolved[0][3] == "chef"
    assert a._exec_cards == {}
    assert (card, THUMB_UP) in talk.unreacted and (card, THUMB_DOWN) in talk.unreacted
    note = talk.posts[-1]["message"]
    assert "Approved by Chef" in note and "running it" in note
    assert a.handled_events == []          # decided in code: no extra model turn


def test_thumbs_down_resolves_deny(monkeypatch):
    a, talk, core, card = _armed(monkeypatch)
    rid = a._exec_cards[f"exec:dmx1:{card}"]["request_id"]
    _react(a, THUMB_DOWN, card)
    assert core.resolved == [("sk-1", "deny", rid, "chef")]
    assert "Denied by Chef" in talk.posts[-1]["message"]


def test_check_mark_also_approves(monkeypatch):
    a, talk, core, card = _armed(monkeypatch)
    _react(a, "✅", card)
    assert core.resolved and core.resolved[0][1] == "once"


def test_other_emoji_on_card_is_decorative(monkeypatch):
    a, talk, core, card = _armed(monkeypatch)
    _react(a, "❤", card)
    assert core.resolved == [] and a._exec_cards and not talk.posts[1:]


def test_unauthorized_reactor_is_refused(monkeypatch):
    a, talk, core, card = _armed(monkeypatch)
    core.refusal = "Spaghet"
    _react(a, THUMB_UP, card)
    assert core.resolved == [] and a._exec_cards          # card stays pending
    assert "Only Spaghet can answer" in talk.posts[-1]["message"]


def test_approve_on_deny_only_card_keeps_it_live(monkeypatch):
    a, talk, core, card = _armed(monkeypatch, _Prompt(choices=("deny",)))
    assert talk.reacted[card] == [THUMB_DOWN]             # only deny offered
    _react(a, THUMB_UP, card)
    assert core.resolved == [] and a._exec_cards
    assert "only deny" in talk.posts[-1]["message"]


def test_second_answer_reports_already_answered(monkeypatch):
    a, talk, core, card = _armed(monkeypatch)
    key = f"exec:dmx1:{card}"
    a._exec_cards[key]["decided"] = True
    _react(a, THUMB_DOWN, card, mid=8001)
    assert core.resolved == []
    assert "already answered" in talk.posts[-1]["message"].lower()


def test_resolve_zero_reports_dead_card(monkeypatch):
    a, talk, core, card = _armed(monkeypatch)
    core.resolves = 0
    _react(a, THUMB_UP, card)
    assert a._exec_cards == {}
    assert "already been answered or timed out" in talk.posts[-1]["message"]


def test_settle_observer_retires_card_from_other_surface(monkeypatch):
    a, talk, core, card = _armed(monkeypatch)
    rid = a._exec_cards[f"exec:dmx1:{card}"]["request_id"]

    async def go():
        # In production the observer fires from the agent thread; here we fire it as a
        # threadsafe call onto the loop that is running, exactly the same hop.
        asyncio.get_running_loop().call_soon_threadsafe(core.observers[rid], "resolved")
        for _ in range(5):
            await asyncio.sleep(0)
        for t in list(a._background_tasks):
            await t
    asyncio.run(go())
    assert a._exec_cards == {}
    assert (card, THUMB_UP) in talk.unreacted
    assert len(talk.posts) == 1             # only the card: retirement stays quiet,
    #                                       the runner edits the card itself


def test_reaction_on_normal_message_still_a_yes_no_turn(monkeypatch):
    a, talk, core = _adapter(), Talk(), ApprovalCore(monkeypatch)
    _wire(a, talk)

    async def go():
        await a._on_message("dmx1", {**_react_msg("✅", "940")})
    asyncio.run(go())
    assert a.handled_events and "[Reaction]" in a.handled_events[0].text


def _react_msg(emoji, parent_id):
    return {"id": 8100, "actorId": "chef", "actorType": "users", "actorDisplayName": "Chef",
            "message": emoji, "messageType": "system", "systemMessage": "reaction",
            "messageParameters": {}, "timestamp": 1700000000,
            "parent": {"id": parent_id, "actorId": "hermes-bot", "actorType": "users",
                       "message": "shall I?", "messageParameters": {}}}


def test_emoji_mapping_shape():
    assert set(EXEC_APPROVAL_EMOJIS) == {THUMB_UP, "✅", "✔", THUMB_DOWN, "❌", "✖"}
    assert all(v in ("once", "deny") for v in EXEC_APPROVAL_EMOJIS.values())
