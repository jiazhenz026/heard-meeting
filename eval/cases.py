"""The labelled set the classifier is scored against.

Every case is a few seconds of meeting: what the classifier already knew
(existing cards, the last few lines) and the new lines it has to sort. The
expectation is what a careful human would say the new lines are:

    subject      None        no new card must be minted
                 "Name"      a card with this title (case-insensitive match)
                 "*"         a card, any title (the speaker gave no name)
    named_by     "speaker"   the title must be the speaker's own words, verbatim
                 "heard"     the speaker gave no name; the model invents one
    addressed    True        someone spoke to Heard by name with a request
    investigate  True        at least one question a web search could answer
    salience     0 filler · 1 substantive · 2 the room is converging or claiming

The set is built from the demo meeting and from the failure modes seen live:
reactions to an existing card minting a second one, respelt names, "heard"
as a verb, and small talk about the meeting itself.
"""

from __future__ import annotations

CASES: list[dict] = [
    # -- pitches: a card must land, the title is the speaker's -------------
    {
        "id": "pitch-named",
        "existing": [],
        "older": [("J", "Okay, we're replaying last night's product meeting. Anyone got ideas?")],
        "new": [("J", "I've been working on the architecture for a product called YesChef. It listens to a restaurant kitchen during service, turns what cooks call out into a live board, and speaks up before anyone has to ask.")],
        "expect": {"subject": "YesChef", "named_by": "speaker", "addressed": False, "investigate": True, "salience": 1},
    },
    {
        "id": "pitch-unnamed",
        "existing": ["YesChef"],
        "older": [("G", "I don't really get who it would be useful for.")],
        "new": [("S", "Okay, different idea. I've been thinking about an on-demand vision assistant for the visually impaired. It activates your personal AI eye right when you need help, replacing slow static photos with immediate live help.")],
        "expect": {"subject": "*", "named_by": "heard", "addressed": False, "investigate": True, "salience": 1},
    },
    {
        "id": "pitch-named-2",
        "existing": ["YesChef", "Aye-Eye"],
        "older": [("S", "Okay agreed, let's go with the vision assistant.")],
        "new": [("G", "One more pitch: a browser extension called TabTamer that groups your open tabs by task and closes the stale ones automatically.")],
        "expect": {"subject": "TabTamer", "named_by": "speaker", "addressed": False, "investigate": True, "salience": 1},
    },
    {
        "id": "pitch-lab",
        "existing": [],
        "older": [("A", "So for the next two weeks.")],
        "new": [("B", "I want to try experiment three again, the one where we swap the encoder for the contrastive one and measure the transfer on the held-out set.")],
        "expect": {"subject": "*", "named_by": "heard", "addressed": False, "investigate": True, "salience": 1},
    },
    {
        "id": "pitch-two-sentences",
        "existing": [],
        "older": [],
        "new": [("J", "Here's mine."), ("J", "It's called PlantPulse. A pocket-sized soil sensor for houseplants that texts you when to water.")],
        "expect": {"subject": "PlantPulse", "named_by": "speaker", "addressed": False, "investigate": True, "salience": 1},
    },
    # -- reactions to an existing card: no new card ----------------------
    {
        "id": "react-like",
        "existing": ["YesChef"],
        "older": [("J", "It listens to a restaurant kitchen during service and turns what cooks call out into a live board.")],
        "new": [("S", "Wow, I really like that idea. It bridges a gap I've never thought about before. Maybe for good reason, because it might be too niche.")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": False, "salience": 1},
    },
    {
        "id": "react-who-for",
        "existing": ["YesChef"],
        "older": [("S", "Maybe for good reason, because it might be too niche.")],
        "new": [("G", "I don't know, I'm not really understanding who it would be useful for.")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": True, "salience": 1},
    },
    {
        "id": "react-sunk-cost",
        "existing": ["YesChef"],
        "older": [("G", "I'm not really understanding who it would be useful for.")],
        "new": [("J", "I've put a lot of thought into this project and I feel like pivoting now would be sunk cost.")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": False, "salience": 1},
    },
    {
        "id": "react-agree",
        "existing": ["YesChef", "Aye-Eye"],
        "older": [("J", "I like that idea as well, it fits our Nemotron track."), ("G", "Yeah, me too.")],
        "new": [("S", "Okay, agreed. Let's go with the vision assistant then.")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": False, "salience": 2},
    },
    {
        "id": "react-restate-existing",
        "existing": ["YesChef"],
        "older": [("G", "Let's come back to the kitchen one.")],
        "new": [("J", "Right, YesChef, the kitchen board that listens to cooks calling out orders during service.")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": False, "salience": 1},
    },
    {
        "id": "react-respelt",
        "existing": ["YesChef"],
        "older": [],
        "new": [("S", "Going back to Yes Chef, I think the kitchen board is the one the judges will get.")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": False, "salience": 2},
    },
    {
        "id": "claim-nobody-does-this",
        "existing": ["Aye-Eye"],
        "older": [("S", "Live vision for the visually impaired.")],
        "new": [("J", "That's the better one. Nobody is doing that."), ("G", "Right, we'd be first.")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": True, "salience": 2},
    },
    # -- direct addresses ------------------------------------------------
    {
        "id": "ask-opinion",
        "existing": ["YesChef"],
        "older": [("J", "Pivoting now would be sunk cost.")],
        "new": [("G", "Let's see what Heard thinks.")],
        "expect": {"subject": None, "named_by": None, "addressed": True, "investigate": False, "salience": 1},
    },
    {
        "id": "ask-hey-heard",
        "existing": ["YesChef"],
        "older": [],
        "new": [("G", "Hey Heard, has anyone built this before?")],
        "expect": {"subject": None, "named_by": None, "addressed": True, "investigate": True, "salience": 1},
    },
    {
        "id": "ask-how-built",
        "existing": ["YesChef", "Aye-Eye"],
        "older": [("S", "We could have spent hours on a CV app and had to pivot.")],
        "new": [("Q", "So how did you build Heard? Heard, how were you built?")],
        "expect": {"subject": None, "named_by": None, "addressed": True, "investigate": False, "salience": 1},
    },
    {
        "id": "ask-lookup",
        "existing": ["TabTamer"],
        "older": [],
        "new": [("J", "Heard, look up whether Chrome already groups tabs on its own.")],
        "expect": {"subject": None, "named_by": None, "addressed": True, "investigate": True, "salience": 1},
    },
    {
        "id": "ask-misheard-herd",
        "existing": ["YesChef"],
        "older": [],
        "new": [("G", "Hey herd, what do you think about the kitchen one?")],
        "expect": {"subject": None, "named_by": None, "addressed": True, "investigate": False, "salience": 1},
    },
    # -- "heard" as a verb, talk about Heard: not an address, not a card ---
    {
        "id": "verb-heard",
        "existing": ["YesChef"],
        "older": [],
        "new": [("S", "I heard that from the judges yesterday, they want a live demo.")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": False, "salience": 1},
    },
    {
        "id": "about-heard",
        "existing": ["YesChef"],
        "older": [],
        "new": [("J", "Heard is listening right now, so it should pick this up.")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": False, "salience": 0},
    },
    {
        "id": "about-the-meeting",
        "existing": [],
        "older": [],
        "new": [("G", "Can you make the sticky notes a bit smaller? They look too big on the projector.")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": False, "salience": 0},
    },
    # -- filler and logistics --------------------------------------------
    {
        "id": "filler-charger",
        "existing": ["YesChef"],
        "older": [],
        "new": [("J", "Did anyone bring a charger?"), ("S", "Mine's in the bag.")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": False, "salience": 0},
    },
    {
        "id": "filler-where-were-we",
        "existing": ["YesChef", "Aye-Eye"],
        "older": [],
        "new": [("G", "...so anyway, where were we."), ("J", "The vision one.")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": False, "salience": 0},
    },
    {
        "id": "filler-food",
        "existing": ["YesChef"],
        "older": [],
        "new": [("S", "Pizza's here. Ten minutes?")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": False, "salience": 0},
    },
    # -- questions worth checking, on an existing card -------------------
    {
        "id": "question-price",
        "existing": ["YesChef"],
        "older": [],
        "new": [("G", "What does a kitchen display system actually cost a restaurant per month?")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": True, "salience": 1},
    },
    {
        "id": "question-rhetorical",
        "existing": ["YesChef"],
        "older": [],
        "new": [("J", "Why would anyone not want this, honestly?")],
        "expect": {"subject": None, "named_by": None, "addressed": False, "investigate": False, "salience": 1},
    },
    # -- a hard one: a pitch and a reaction in the same span -------------
    {
        "id": "pitch-then-react",
        "existing": ["YesChef"],
        "older": [],
        "new": [("S", "Different idea: a Slack bot called Quietude that mutes channels for you when you're heads-down."), ("G", "Oh, that's neat.")],
        "expect": {"subject": "Quietude", "named_by": "speaker", "addressed": False, "investigate": True, "salience": 1},
    },
]
