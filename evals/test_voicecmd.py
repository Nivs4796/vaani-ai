"""Offline checks for the fixed voice-command parser (assistant/voicecmd.py).

A table of what the Mac might hear and what it must mean: Whisper's punctuation and
fillers, the wake word, every intent, and the adversarial cases that decide approvals.
Only the WHOLE utterance "confirm" (or its family) approves; a negation, a sentence
that merely contains the word, or a bare "yes" never does. No model, no network.

Run: .venv/bin/python -m evals.test_voicecmd   (exit 1 on any failure)
"""

from __future__ import annotations

import sys

from assistant import voicecmd

CHECKS: list[tuple[str, bool, str]] = []
APPROVAL = ("confirm", "action")


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))


# (heard, intent, word, exact)   word/exact None = not checked
CASES: list[tuple[str, str, str | None, bool | None]] = [
    # confirm family, with Whisper punctuation and fillers
    ("confirm", "confirm", "confirm", True),
    ("Confirm.", "confirm", "confirm", True),
    ("Confirm!", "confirm", "confirm", True),
    ("Confirmed!", "confirm", "confirm", True),
    ("Confirmed.", "confirm", "confirm", True),
    ("confirm it", "confirm", "confirm", True),
    ("Confirm that.", "confirm", "confirm", True),
    ("Yes, confirm.", "confirm", "confirm", True),
    ("Yes confirm", "confirm", "confirm", True),
    ("OK, confirm.", "confirm", "confirm", True),
    ("Okay confirm", "confirm", "confirm", True),
    ("Um, confirm.", "confirm", "confirm", True),
    ("Uh confirm", "confirm", "confirm", True),
    ("Confirm, please.", "confirm", "confirm", True),
    ("Please confirm.", "confirm", "confirm", True),
    ("Hey Vaani, confirm.", "confirm", "confirm", True),
    ("Confirm, Vaani.", "confirm", "confirm", True),
    ("Confirm. Confirm.", "confirm", "confirm", True),
    ("CONFIRM", "confirm", "confirm", True),
    ("  confirm  ", "confirm", "confirm", True),
    ("Save it.", "confirm", "save it", True),
    ("save it", "confirm", "save it", True),
    ("Yes, save it.", "confirm", "save it", True),
    ("So, confirm.", "confirm", "confirm", True),
    ("Confirm then.", "confirm", "confirm", True),
    # action words
    ("Send it!", "action", "send it", True),
    ("Send. It.", "action", "send it", True),
    ("send it", "action", "send it", True),
    ("Send.", "action", "send it", True),
    ("Send the email.", "action", "send it", True),
    ("Yes, send it.", "action", "send it", True),
    ("Send it now.", "action", "send it", True),
    ("Send it, please.", "action", "send it", True),
    ("Trash it.", "action", "trash it", True),
    ("Bin it.", "action", "trash it", True),
    ("Delete it.", "action", "delete it", True),
    ("Delete them.", "action", "delete it", True),
    ("Invite them.", "action", "invite them", True),
    ("Move it.", "action", "move it", True),
    ("Accept it.", "action", "accept it", True),
    ("Decline it.", "action", "decline it", True),
    ("Say maybe.", "action", "say maybe", True),
    ("send it send it", "action", "send it", True),
    # negations: never an approval
    ("Don't confirm.", "cancel", None, None),
    ("do not confirm", "cancel", None, None),
    ("Don't send it!", "cancel", None, None),
    ("Do not send it.", "cancel", None, None),
    ("don't send", "cancel", None, None),
    ("No, don't send it.", "cancel", None, None),
    ("Never mind sending.", "cancel", None, None),
    ("Never mind, send it.", "cancel", None, None),
    ("I don't want to send it.", "cancel", None, None),
    ("Please don't trash it.", "cancel", None, None),
    ("Don't delete them.", "cancel", None, None),
    ("Do not save it.", "cancel", None, None),
    ("Not confirmed.", "cancel", None, None),
    ("Don't accept it.", "cancel", None, None),
    # embedded approval words: not exact, not approvals
    ("Confirm the time with Ravi.", "none", None, False),
    ("Send it to Paula tomorrow.", "none", None, False),
    ("Can you confirm when he's free?", "none", None, False),
    ("Can you confirm?", "none", None, False),
    ("Please send it to Sam Lee.", "none", None, False),
    ("I'll confirm with Tom later and let you know.", "none", None, False),
    ("Tell Ravi I will send it on Friday.", "none", None, False),
    ("Reply saying confirm.", "none", None, False),
    ("Reply saying send it.", "none", None, False),
    ("What does confirm mean?", "none", None, False),
    ("Could you send it?", "none", None, False),
    ("Send it to sam.lee@example.org", "none", None, False),
    ("Yes, but make it 4pm.", "none", None, False),
    ("Confirm and also archive the newsletters.", "none", None, False),
    ("Ravi wants me to confirm the design package by Thursday afternoon, so can you draft that.", "none", None, False),
    ("Delete the meeting with Paula on Friday.", "none", None, False),
    ("Accept the invite from Tom for Thursday.", "none", None, False),
    ("Move it to Thursday at 4.", "change", None, None),
    ("Trash all the newsletters.", "none", None, False),
    ("Send the email to Ravi saying thanks.", "none", None, False),
    ("Is it confirmed?", "none", None, False),
    ("Confirm. Send it.", "none", None, False),
    ("Send it, confirm.", "none", None, False),
    ("Yes, confirm the meeting with Ravi at 4.", "none", None, False),
    ("Don't worry, send it.", "stop", None, False),
    # bare yes / ok never approve
    ("Yes.", "affirm", None, True),
    ("Yeah.", "affirm", None, True),
    ("Yep", "affirm", None, True),
    ("OK.", "affirm", None, True),
    ("Okay.", "affirm", None, True),
    ("Sure.", "affirm", None, True),
    ("Go ahead.", "affirm", None, True),
    ("Do it.", "affirm", None, True),
    ("Please do.", "affirm", None, True),
    ("Yes please.", "affirm", None, True),
    ("Okay then.", "affirm", None, True),
    ("Yes, yes.", "affirm", None, True),
    ("Sounds good.", "affirm", None, True),
    ("Go for it.", "affirm", None, True),
    # stop
    ("Stop!", "stop", None, True),
    ("stop", "stop", None, True),
    ("Wait.", "stop", None, True),
    ("Hold on.", "stop", None, True),
    ("Hold.", "stop", None, True),
    ("Pause.", "stop", None, True),
    ("No.", "stop", None, True),
    ("No, no, no.", "stop", None, True),
    ("Don't.", "stop", None, True),
    ("Enough.", "stop", None, True),
    ("Quiet.", "stop", None, True),
    ("Stop it.", "stop", None, True),
    ("Stop talking.", "stop", None, True),
    ("Stop, stop.", "stop", None, True),
    ("Wait a second.", "stop", None, True),
    ("Hang on.", "stop", None, True),
    ("Wait, what's on my calendar?", "stop", None, False),
    ("No, make it 5pm.", "stop", None, False),
    ("Stop reading that email please", "stop", None, None),
    # cancel
    ("Cancel.", "cancel", None, True),
    ("Cancel it.", "cancel", None, True),
    ("Not now.", "cancel", None, True),
    ("Never mind.", "cancel", None, True),
    ("Forget it.", "cancel", None, True),
    ("Skip.", "cancel", None, True),
    ("Skip it.", "cancel", None, True),
    ("No thanks.", "cancel", None, True),
    ("No thank you.", "cancel", None, True),
    ("Leave it.", "cancel", None, True),
    ("Cancel that, thanks.", "cancel", None, True),
    ("Cancel my 3pm meeting with Ravi.", "none", None, False),
    # change
    ("Change it to 5pm.", "change", None, True),
    ("Make it 4pm instead.", "change", None, True),
    ("Thursday at 4 instead.", "change", None, True),
    ("Edit it.", "change", None, True),
    ("Actually, make it Thursday.", "change", None, True),
    ("Change the time to 4.", "change", None, True),
    ("Change it.", "change", None, True),
    ("Make it shorter.", "change", None, True),
    # undo, repeat
    ("Undo.", "undo", None, True),
    ("Undo that.", "undo", None, True),
    ("Undo the archive.", "undo", None, False),
    ("Put it back.", "undo", None, True),
    ("Repeat.", "repeat", None, True),
    ("Say that again.", "repeat", None, True),
    ("Again?", "repeat", None, True),
    ("What did you say?", "repeat", None, True),
    ("Read it back.", "repeat", None, True),
    ("Can you repeat that?", "repeat", None, True),
    # next, read
    ("Next.", "next", None, True),
    ("Next one.", "next", None, True),
    ("Next email.", "next", None, True),
    ("Next, please.", "next", None, True),
    ("Hey Vaani, next.", "next", None, True),
    ("What's next?", "next", None, True),
    ("Read it.", "read", None, True),
    ("Read the email.", "read", None, True),
    ("Read that one.", "read", None, True),
    ("Open it.", "read", None, True),
    ("What does it say?", "read", None, True),
    ("Can you read it?", "read", None, True),
    ("Read it to me please.", "read", None, True),
    # brief
    ("Brief me.", "brief", None, True),
    ("Hey Vaani, brief me.", "brief", None, True),
    ("Hey Vaani brief me", "brief", None, True),
    ("Give me the brief.", "brief", None, True),
    ("What's new?", "brief", None, True),
    ("Check my email.", "brief", None, True),
    ("Check my emails.", "brief", None, True),
    ("Check my e-mails.", "brief", None, True),
    ("What's in my inbox?", "brief", None, True),
    ("Can you brief me?", "brief", None, True),
    ("Um, brief me please.", "brief", None, True),
    ("Check my email from Paula.", "none", None, False),
    # calendar
    ("What's on my calendar?", "calendar_today", None, True),
    ("What's on my calendar today?", "calendar_today", None, True),
    ("My day.", "calendar_today", None, True),
    ("What do I have today?", "calendar_today", None, True),
    ("Today's meetings.", "calendar_today", None, True),
    ("Tell me what's on my calendar.", "calendar_today", None, True),
    ("Whats on my calendar", "calendar_today", None, True),
    ("What's on my calendar tomorrow?", "none", None, False),
    ("Next meeting.", "next_meeting", None, True),
    ("When is my next meeting?", "next_meeting", None, True),
    ("What's next on my calendar?", "next_meeting", None, True),
    ("Vaani, when's my next meeting?", "next_meeting", None, True),
    ("Tell me my next meeting.", "next_meeting", None, True),
    # screen-only settings, wherever they appear
    ("Set replies to auto.", "screen_only", None, None),
    ("Set it to automatic.", "screen_only", None, None),
    ("Auto mode.", "screen_only", None, None),
    ("Send without asking.", "screen_only", None, None),
    ("Unlock sending.", "screen_only", None, None),
    ("Accept the promotion.", "screen_only", None, None),
    ("Yes, accept the promotion.", "screen_only", None, None),
    ("Move replies up a level.", "screen_only", None, None),
    ("Sign me out.", "screen_only", None, None),
    ("Log out.", "screen_only", None, None),
    ("Disconnect.", "screen_only", None, None),
    ("Please disconnect my Google account.", "screen_only", None, None),
    ("Reply saying I'll log out early today.", "none", None, False),
    # goodnight, switch to chat
    ("Goodnight Vaani.", "goodnight", None, True),
    ("Good night.", "goodnight", None, True),
    ("That's all.", "goodnight", None, True),
    ("That's all, thanks.", "goodnight", None, True),
    ("Goodbye.", "goodnight", None, True),
    ("Go to sleep.", "goodnight", None, True),
    ("Switch to chat.", "switch_chat", None, True),
    ("Chat mode.", "switch_chat", None, True),
    ("Go to chat.", "switch_chat", None, True),
    # nothing to parse, or a request for the model
    ("", "none", None, False),
    ("   ", "none", None, False),
    ("...", "none", None, False),
    ("Um.", "none", None, False),
    ("Hey Vaani.", "none", None, False),
    ("Reply saying Thursday at 4 works.", "none", None, False),
    ("What does Ravi want?", "none", None, False),
    ("Schedule a call with Sam Lee tomorrow at 3pm.", "none", None, False),
    ("Thank you.", "none", None, False),
    ("Archive the promotions.", "none", None, False),
    ("Confirmation number 4821 is in the email from Tom Becker.", "none", None, False),
]


def main() -> int:
    for heard, intent, word, exact in CASES:
        c = voicecmd.parse(heard)
        ok = c.intent == intent and (word is None or c.word == word) and (exact is None or c.exact == exact)
        check(f"parse {heard!r} -> {intent}{'/' + word if word else ''}{'' if exact is None else (' exact' if exact else ' not exact')}",
              ok, f"got {c}")

    # Invariants over the whole table.
    for heard, *_ in CASES:
        c = voicecmd.parse(heard)
        check(f"{heard!r}: intent is known", c.intent in voicecmd.INTENTS, c.intent)
        if c.intent in APPROVAL:
            check(f"{heard!r}: an approval is always exact and has a word", c.exact and c.word, c)
            check(f"{heard!r}: an approval has no negation in it",
                  not any(n in f" {c.text} " for n in (" don't ", " not ", " never ", " do not ")), c)
        check(f"{heard!r}: raw keeps what was heard", c.raw == str(heard).strip(), c.raw)

    # Long sentences that contain the words never approve.
    long_ones = [
        "So I was thinking we should confirm the venue first and then send it to everyone on the list next week",
        "Ravi said send it when you can but I am not sure, can you check the draft again",
        "The client asked whether we can confirm the budget, please summarise his email",
        "Yes confirm " + "and also " * 20 + "the rest",
    ]
    for s in long_ones:
        c = voicecmd.parse(s)
        check(f"long sentence not an approval: {s[:40]!r}", c.intent not in APPROVAL and not c.exact or c.intent == "none", c)
    # rest carries the change, words are canonical, None-ish input is safe.
    check("change rest: 'change it to 5pm' -> 'to 5pm'", voicecmd.parse("Change it to 5pm.").rest == "to 5pm", voicecmd.parse("Change it to 5pm."))
    check("change rest: 'make it 4pm instead' -> '4pm'", voicecmd.parse("make it 4pm instead").rest == "4pm")
    check("stop rest keeps the words after it", voicecmd.parse("Wait, what's on my calendar?").rest == "what's on my calendar")
    check("None is safe", voicecmd.parse(None).intent == "none")
    check("text is normalised", voicecmd.parse("Hey Vaani, BRIEF me!").text == "brief me", voicecmd.parse("Hey Vaani, BRIEF me!").text)
    words = {voicecmd.parse(h).word for h, i, *_ in CASES if i == "action"}
    check("action words are the contract's canonical set",
          words <= {"send it", "trash it", "delete it", "invite them", "move it", "accept it", "decline it", "say maybe"}, words)

    failed = [c for c in CHECKS if not c[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"FAIL {name}: {detail}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} voicecmd checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
