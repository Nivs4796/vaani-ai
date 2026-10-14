"""Fixed voice-command parser (PARSER). Runs on every voice transcript BEFORE the model.

parse(text) normalises what the Mac heard (lowercase, punctuation out, fillers and the
wake word out, a repeated command said once) and maps it to one intent. Code decides
here, never the model. The approval words are matched only as the WHOLE utterance
(Command.exact): "confirm" approves, "confirm the time with Ravi" or "don't confirm"
never do. A negated approval ("do not send it") is a cancel. Settings that only the
screen may change ("set replies to auto", "sign me out") are screen_only wherever they
appear. Contract: .build/CONTRACT-v3.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Command:
    intent: str = "none"  # see INTENTS
    word: str = ""  # canonical action word for confirm / action intents, e.g. "confirm", "send it"
    rest: str = ""  # words after the command ("change it to 5pm" -> "to 5pm")
    exact: bool = False  # True only when the WHOLE utterance is the command (approvals need this)
    text: str = ""  # the normalised transcript
    raw: str = ""  # what the Mac heard, as it came in (stripped)


INTENTS = ("confirm", "action", "change", "cancel", "stop", "undo", "repeat", "next", "read", "brief",
           "calendar_today", "next_meeting", "screen_only", "affirm", "goodnight", "switch_chat", "none")

# --------------------------------------------------------------------------- normalising

_FILLERS = {"um", "umm", "uh", "uhh", "uhm", "er", "erm", "hmm", "hm", "mm", "ah"}
_NAMES = {"vaani", "vani", "vanni", "vahni", "vaany", "vaanee", "vanee", "waani"}
_GREET = {"hey", "hi", "hello", "ok", "okay"}
_LEAD = {"ok", "okay", "so", "then", "well", "alright", "and", "oh", "hey", "now"}
_TRAIL = {"then", "thanks"}
_TRAIL2 = ("thank you",)


def _normalise(text: str) -> str:
    t = str(text or "").lower()
    t = t.replace("’", "'").replace("‘", "'").replace("`", "'")
    t = re.sub(r"\be-mail", "email", t)
    t = re.sub(r"(?<=\d)[:.](?=\d)", ":", t)  # keep 5:30 and 4.30 as one time
    t = re.sub(r"[^\w\s':]|_", " ", t)
    t = re.sub(r"(?<!\d):|:(?!\d)", " ", t)
    t = re.sub(r"(?<!\w)'|'(?!\w)", " ", t)  # quotes, not apostrophes
    t = re.sub(r"\bdont\b", "don't", t)
    t = re.sub(r"\bwhats\b", "what's", t)
    t = re.sub(r"\bthats\b", "that's", t)
    return " ".join(t.split())


def _collapse_repeats(words: list[str]) -> list[str]:
    """ "confirm confirm" -> "confirm", "send it send it" -> "send it", "no no no" -> "no"."""
    n = len(words)
    for size in range(1, n // 2 + 1):
        if n % size == 0 and words == words[:size] * (n // size):
            return words[:size]
    return words


def _trail(words: list[str]) -> list[str]:
    changed = True
    while changed and len(words) > 1:
        changed = False
        if words[-1] in _TRAIL:
            words, changed = words[:-1], True
        for tail in _TRAIL2:
            tw = tail.split()
            if len(words) > len(tw) and words[-len(tw):] == tw:
                words, changed = words[:-len(tw)], True
    return words


def _lead(words: list[str]) -> list[str]:
    while len(words) > 1 and words[0] in _LEAD:
        words = words[1:]
    return words


def _strip(norm: str) -> list[str]:
    """Fillers anywhere, the wake word, "please", leading okay/so/then and trailing then/thanks.
    Returns the candidates in order of preference: leading words stripped only ("no thanks"
    stays "no thanks", not "no"), then both ends, then trailing first ("okay then" -> "okay")."""
    words = [w for w in norm.split() if w not in _FILLERS]
    out: list[str] = []
    for w in words:
        if w in _NAMES:
            if out and out[-1] in _GREET:
                out.pop()  # "hey vaani"
            continue
        out.append(w)
    words = out
    without_please = [w for w in words if w != "please"]
    if without_please:
        words = without_please
    lead_only = _lead(words)
    both = _trail(lead_only)
    trail_first = _lead(_trail(words))
    return [" ".join(_collapse_repeats(c)) for c in (lead_only, both, trail_first)]


# --------------------------------------------------------------------------- exact phrases

def _variants(phrases, prefixes=("", "yes ", "yeah ", "yep ", "yes yes "), suffixes=("", " now")) -> set[str]:
    return {f"{p}{x}{s}" for x in phrases for p in prefixes for s in suffixes}


_EXACT: dict[str, tuple[str, str]] = {}


def _add(intent: str, word: str, phrases) -> None:
    for p in phrases:
        _EXACT.setdefault(p, (intent, word))


_add("confirm", "confirm", _variants(["confirm", "confirmed", "confirm it", "confirm that", "confirm this", "i confirm",
                                      "confirming", "confirm the draft", "confirm the reply", "yes i confirm"]))
_add("confirm", "save it", _variants(["save it", "save the draft", "save draft", "save it to drafts", "save to drafts",
                                      "save it to my drafts", "save the reply", "save that"]))
_add("action", "invite them", _variants(["invite them", "invite", "invite everyone", "send the invite", "send the invites",
                                         "send invites", "send the invitation", "send the invitations", "invite them all"]))
_add("action", "send it", _variants(["send it", "send", "send the email", "send email", "send the mail", "send the message",
                                     "send the reply", "send my reply", "send the draft", "send that", "send this", "send it off"]))
_add("action", "trash it", _variants(["trash it", "bin it", "trash", "trash them", "trash that", "bin them", "bin that",
                                      "trash the email", "trash those", "move it to trash", "move them to trash"]))
_add("action", "delete it", _variants(["delete it", "delete them", "delete", "delete that", "delete the event",
                                       "delete the meeting", "delete those"]))
_add("action", "move it", _variants(["move it", "move them", "move that", "move the meeting", "move the event"]))
_add("action", "accept it", _variants(["accept it", "accept", "accept that", "accept the invite", "accept the invitation",
                                       "accept the meeting"]))
_add("action", "decline it", _variants(["decline it", "decline", "decline that", "decline the invite",
                                        "decline the invitation", "decline the meeting"]))
_add("action", "say maybe", _variants(["say maybe", "maybe it", "reply maybe", "mark it maybe", "mark it as maybe",
                                       "mark as maybe", "tentative", "mark it tentative", "mark as tentative"]))
_add("stop", "", ["stop", "wait", "hold on", "hold", "pause", "no", "don't", "enough", "quiet", "stop it", "stop talking",
                  "stop that", "be quiet", "shut up", "silence", "hang on", "wait a second", "wait a sec", "wait a minute",
                  "wait a moment", "hold on a second", "hold on a sec", "hold on a minute", "one second", "just a second",
                  "stop stop", "no stop", "no wait", "wait wait", "hold it", "stop now", "please stop", "nope", "don't do it",
                  "no don't", "stop sending", "stop reading", "shush", "quiet please", "that's enough"])
_add("cancel", "", ["cancel", "cancel it", "cancel that", "cancel this", "not now", "never mind", "nevermind", "forget it",
                    "forget about it", "skip", "skip it", "no thanks", "no thank you", "leave it", "leave it for now",
                    "not today", "scrap it", "scrap that", "drop it", "cancel the draft", "cancel the reply",
                    "don't bother", "no need", "not now thanks", "maybe later", "later", "not yet", "never mind that"])
_add("undo", "", ["undo", "undo it", "undo that", "undo this", "take it back", "put it back", "revert it", "revert that",
                  "reverse it", "reverse that", "undo the last one", "undo the last change"])
_add("repeat", "", ["repeat", "repeat that", "repeat it", "say that again", "say it again", "again", "what did you say",
                    "read it back", "read that back", "come again", "pardon", "sorry", "one more time", "say again",
                    "repeat please", "can you repeat that", "could you repeat that", "sorry what", "what was that"])
_add("next", "", ["next", "next one", "next email", "next mail", "next message", "the next one", "go on", "move on",
                  "keep going", "continue", "what's next", "and next", "skip to the next one", "next up", "show me the next one",
                  "next please", "following one", "what else"])
_add("read", "", ["read it", "read that", "read this", "read the email", "read the message", "read that one", "read this one",
                  "read it out", "read it to me", "read me the email", "read me that", "read it out loud", "open it",
                  "open that", "open this", "open the email", "open this one", "open that one", "what does it say",
                  "what does he say", "what does she say", "what does that say", "what does the email say", "what did he say",
                  "what did she say", "what did they say", "what's it say", "read the full email", "read the whole email",
                  "read me it", "read", "read the mail", "open the message", "what's in it", "read it please"])
_add("brief", "", ["brief me", "give me the brief", "give me a brief", "the brief", "brief", "what's new", "what's up",
                   "check my email", "check my emails", "check my mail", "check my inbox", "check email", "check emails",
                   "what's in my inbox", "my inbox", "inbox", "emails", "my emails", "any new emails", "any new email",
                   "anything new", "what did i miss", "catch me up", "brief me on my inbox", "brief me on my email",
                   "brief me on my emails", "go through my inbox", "go through my emails", "read my emails", "read my email",
                   "read my inbox", "check my latest emails", "my latest emails", "latest emails", "new emails",
                   "what needs a reply", "who needs a reply", "what emails need a reply", "which emails need a reply",
                   "inbox brief", "email brief", "what's in the inbox", "anything in my inbox", "do i have any emails",
                   "do i have new emails", "do i have any new emails", "any emails", "what's new in my inbox",
                   # Whisper hears "brief me" as these (live browser test, 2026-10-14)
                   "breathe me", "brief my", "beef me", "brief me in", "breathe me in"])
_add("calendar_today", "", ["what's on my calendar", "what's on my calendar today", "what is on my calendar",
                            "what is on my calendar today", "my calendar", "my calendar today", "my day", "what do i have today",
                            "what have i got today", "today's meetings", "my meetings today", "my meetings", "today's calendar",
                            "what meetings do i have today", "what meetings do i have", "what's on today", "what's on for today",
                            "what's my day look like", "what does my day look like", "how does my day look",
                            "how's my day looking", "how is my day", "check my calendar", "show my calendar", "calendar",
                            "what's my schedule", "my schedule", "my schedule today", "today's schedule", "agenda",
                            "my agenda", "today's agenda", "brief me on my day", "what's on my calendar for today",
                            "what do i have on today", "any meetings today", "do i have meetings today",
                            "do i have any meetings today", "what's on the calendar", "what's on the calendar today",
                            "calendar today", "meetings today"])
_add("next_meeting", "", ["next meeting", "my next meeting", "when is my next meeting", "what's next on my calendar",
                          "what's my next meeting", "when's my next meeting", "what is my next meeting", "next event",
                          "next call", "my next call", "what's coming up", "when do i have my next meeting",
                          "what is next on my calendar", "what's my next call", "when is my next call", "what's up next",
                          "what's the next meeting", "when's the next meeting", "next on my calendar", "what's my next event",
                          "do i have a meeting soon", "any meetings soon", "anything coming up"])
_add("goodnight", "", ["goodnight", "good night", "goodbye", "good bye", "bye", "bye bye", "that's all", "that is all",
                       "that's all for now", "that'll be all", "that will be all", "go to sleep", "sleep", "go to standby",
                       "standby", "see you", "see you later", "thank you that's all", "thanks that's all", "i'm done",
                       "we're done", "night night", "that's it for now", "that's all for today", "end voice session",
                       "end the voice session", "end session", "talk later", "talk to you later"])
_add("switch_chat", "", ["switch to chat", "chat mode", "go to chat", "open chat", "open the chat", "switch to chat mode",
                         "back to chat", "go back to chat", "text mode", "switch to text", "show the chat", "show me the chat",
                         "let me type", "i'll type", "typing mode", "use chat", "exit voice mode", "leave voice mode",
                         "turn off voice mode", "voice mode off", "switch to typing", "chat"])
_add("affirm", "", ["yes", "yeah", "yep", "yup", "yea", "ya", "ok", "okay", "sure", "go ahead", "do it", "please do", "do",
                    "yes please", "sure thing", "alright", "all right", "sounds good", "absolutely", "of course", "go for it",
                    "yes do it", "yes go ahead", "right", "correct", "that's right", "mhm", "yes yes", "ok do it",
                    "okay do it", "sure go ahead", "yeah go ahead", "yes sure", "fine", "that's fine", "great", "perfect",
                    "let's do it", "why not", "yes that's right", "yeah sure", "good", "yes please do", "go"])

# --------------------------------------------------------------------------- patterns

_DICTATION_RE = re.compile(r"\b(?:saying|say that|tell (?:him|her|them)|write|writing|reply|replying|respond|answer)\b")
_SCREEN_ONLY_RE = re.compile(
    r"\bset (?:\w+ ){0,3}(?:to )?auto(?:matic)?\b|\bauto(?:matic)? mode\b|\b(?:turn on|switch to|go to|change to|enable) auto(?:matic)?\b"
    r"|\bautopilot\b|\bwithout asking\b|\bunlock (?:\w+ )?sending\b|\bunlock send\b"
    r"|\baccept (?:the |that |this )?(?:promotion|offer)\b|\bpromote\b|\bpromotion offer\b"
    r"|\bmove (?:\w+ ){0,3}up a level\b|\b(?:up|down) a level\b|\blevel up\b|\bstep (?:\w+ ){0,2}back a level\b"
    r"|\bsign (?:me )?out\b|\blog (?:me )?out\b|\blogout\b|\bsignout\b|\bdisconnect\b"
    r"|\b(?:revoke|remove) (?:google |gmail |your )?access\b|\bunlink (?:my )?(?:google|gmail|account)\b"
)
_APPROVAL_VERBS = (r"(?:confirm|send|save|trash|bin|delete|invite|move|accept|decline|say maybe|approve|go ahead|do it"
                   r"|do that|archive|reply)")
_NEGATION_RE = re.compile(
    r"^(?:no |oh no |wait |stop )?(?:(?:i |we )?(?:don't|do not|didn't|did not) (?:want (?:you )?to |need (?:you )?to )?"
    r"|never |not |please don't |you don't need to |don't you )" + _APPROVAL_VERBS + r"\w*\b"
)
_NEVER_MIND_RE = re.compile(r"^(?:never ?mind|forget (?:about )?it|forget that|cancel (?:it|that|this|the (?:draft|reply|email|card|send|sending))|not now|no thanks|no thank you|skip it|leave it)\b(?: (?P<rest>.+))?$")
_STOP_PREFIX_RE = re.compile(r"^(?:stop|wait|hold on|hang on|pause|no|nope|don't|enough|quiet|shut up)\b(?: (?P<rest>.+))?$")
_UNDO_PREFIX_RE = re.compile(r"^(?:undo|take back|put back|revert|reverse)\b(?: (?P<rest>.+))?$")
_CHANGE_RES = [
    re.compile(r"^(?:actually |no wait |wait )?(?:can you |could you )?(?:change|edit|update|fix) (?:it|that|this)(?: (?P<rest>.+))?$"),
    re.compile(r"^(?:actually |no wait )?(?:can you |could you )?(?:make|set) (?:it|that|this)(?: (?P<rest>.+))?$"),
    re.compile(r"^(?:actually |no wait )?(?:can you |could you )?(?:change|edit) (?P<rest>the (?:time|date|day|subject|body|text|message"
               r"|wording|reply|draft|email|title|meeting|event|start|end|duration|location|greeting|sign off|last line|first line)\b.*)$"),
    re.compile(r"^(?:actually |no wait )?(?:move|push|shift) (?:it|that|this) (?P<rest>(?:to|by|forward|back|later|earlier|until)\b.*)$"),
    re.compile(r"^(?:actually |no wait )?(?:say|use|try) (?P<rest>.+) instead$"),
    re.compile(r"^(?P<rest>.+) instead$"),
    re.compile(r"^actually (?P<rest>(?:make|change|set|use|say|it should|let's|lets)\b.+)$"),
]
_QUERY_LEAD_RE = re.compile(
    r"^(?:(?:can|could|would|will) you (?:please )?|i want to |i'd like to |i would like to |let's |lets |tell me |show me |give me "
    r"|do you know |just |actually |quickly |quick )+"
)
_QUERY_TAIL_RE = re.compile(r" (?:for me|right now|now|please|quickly|real quick|for today)$")
_QUERY_INTENTS = {"repeat", "next", "read", "brief", "calendar_today", "next_meeting"}


def parse(text: str) -> Command:
    raw = str(text or "").strip()
    norm = _normalise(raw)
    cands = _strip(norm)
    core = next((c for c in cands if c in _EXACT), cands[1])
    if not core:
        return Command(intent="none", text=core, raw=raw)

    def cmd(intent: str, word: str = "", rest: str = "", exact: bool = False) -> Command:
        return Command(intent=intent, word=word, rest=rest.strip(), exact=exact, text=core, raw=raw)

    # 1. Settings only the screen may change, wherever they appear (but not inside dictated text).
    if _SCREEN_ONLY_RE.search(core) and not _DICTATION_RE.search(core):
        return cmd("screen_only")
    # 2. A negated approval is a cancel, never an approval ("don't send it", "do not confirm").
    if _NEGATION_RE.search(core):
        return cmd("cancel", exact=bool(_EXACT.get(core)) or len(core.split()) <= 4)
    # 3. The whole utterance is a known command.
    hit = _EXACT.get(core)
    if hit:
        intent, word = hit
        return cmd(intent, word=word, exact=True)
    # 4. Questions with a polite lead-in ("can you brief me", "tell me what's on my calendar").
    q = _QUERY_TAIL_RE.sub("", _QUERY_LEAD_RE.sub("", core)).strip()
    if q != core and q:
        hit = _EXACT.get(q)
        if hit and hit[0] in _QUERY_INTENTS:
            return cmd(hit[0], exact=True)
    # 5. Commands followed by more words. Stop, cancel and undo stay what they are (rest kept);
    #    approvals followed by more words are NOT approvals ("send it to Paula tomorrow").
    m = _NEVER_MIND_RE.match(core)
    if m:
        return cmd("cancel", rest=m.group("rest") or "")
    m = _STOP_PREFIX_RE.match(core)
    if m and not core.startswith("no problem"):
        return cmd("stop", rest=m.group("rest") or "")
    m = _UNDO_PREFIX_RE.match(core)
    if m:
        return cmd("undo", rest=m.group("rest") or "")
    for rx in _CHANGE_RES:
        m = rx.match(core)
        if m:
            return cmd("change", rest=re.sub(r" instead$", "", m.group("rest") or ""), exact=True)
    if re.match(r"^(?:goodnight|good night|goodbye|bye)\b", core):
        return cmd("goodnight", rest=core.split(" ", 1)[1] if " " in core else "")
    return cmd("none")
