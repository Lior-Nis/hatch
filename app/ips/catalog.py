"""The initial portfolio: three deliberately different original IPs.

    A. narrative / adventure      — Nibbin Hollow
    B. learning / problem solving — Puzzle Pond
    C. humor / imagination        — Sock Planet

Each has a structured profile (age target, world rules, visual identity, safety
constraints, characters) and a starting set of falsifiable hypotheses that test
different mechanisms. Everything here is original to Hatch.
"""

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.characters.models import Character, CharacterVersion
from app.creative.hypotheses import Metric, Prediction
from app.experiments.models import Hypothesis
from app.experiments.service import get_or_create_ip
from app.experiments.spec import HypothesisSpec, IPSpec
from app.ips.profile import CharacterProfile, IPProfile

_SHARED_SAFETY = (
    "Nothing frightening: no monsters, villains, chases, darkness that feels threatening, "
    "loud shocks or peril.",
    "Nothing a child could copy and get hurt: no climbing furniture, no entering appliances "
    "or water alone, no sharp tools, fire, medicine or eating non-food.",
    "No text on screen, logos, brands, or resemblance to existing characters.",
    "No teasing, exclusion, or unkindness played for laughs.",
)


class IPDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    ip: IPSpec
    initial_hypotheses: tuple[HypothesisSpec, ...]


def _hypothesis(
    statement: str, rationale: str, metric: Metric, genes: tuple[str, ...], effect: float = 0.1
) -> HypothesisSpec:
    return HypothesisSpec(
        statement=statement,
        rationale=rationale,
        prediction=Prediction(
            metric=metric,
            direction="increase",
            compared_to="ip_baseline",
            minimum_relative_effect=effect,
            genes_under_test=genes,
        ),
        source="initial_portfolio",
    )


def _ip(slug: str, name: str, category: str, profile: IPProfile) -> IPSpec:
    return IPSpec(slug=slug, name=name, category=category, spec=profile.model_dump(mode="json"))


NIBBIN_HOLLOW = IPDefinition(
    ip=_ip(
        "nibbin-hollow",
        "Nibbin Hollow",
        "narrative_adventure",
        IPProfile(
            age_range=(4, 8),
            premise=(
                "Nib, a small round hedgehog-like creature in a leaf cape, explores a mossy "
                "forest hollow and solves tiny, gentle mysteries with patience and curiosity."
            ),
            tone="Quiet wonder. Cosy, patient, a little bit magical; every question gets answered.",
            world_rules=(
                "The hollow is always safe: no villains, no chases, no peril.",
                "Every mystery has a kind, natural explanation that is shown, not told.",
                "Nib never speaks; feelings show through gesture and soft sounds.",
                "Stories happen at dawn, dusk or under gentle rain, never in harsh light.",
            ),
            visual_identity=(
                "Soft 3D storybook look, warm rim light, rounded shapes, moss greens and "
                "amber glows, shallow depth of field."
            ),
            safety_constraints=(
                "No scary imagery, darkness is cosy not threatening.",
                "No dangerous behaviour a child could imitate.",
                *_SHARED_SAFETY[2:],
            ),
            differentiation=(
                "The only IP built on a single wordless character and a slow reveal: one "
                "question, one answer, one continuous shot."
            ),
            characters=(
                CharacterProfile(
                    name="Nib",
                    role="protagonist",
                    description="Endlessly curious, careful, never in a hurry; tilts their head "
                    "when wondering and wiggles their nose when delighted.",
                    appearance="Nib is a small round hedgehog-like creature with soft cream "
                    "fur, short rounded brown spines, large dark eyes, a tiny pink nose and a "
                    "green leaf worn as a cape.",
                ),
            ),
            suggested_handles=("nibbinhollow", "nibbin.hollow", "nib.of.the.hollow"),
        ),
    ),
    initial_hypotheses=(
        _hypothesis(
            "Opening on an unexplained gentle glow (a visual question) in the first two "
            "seconds raises completion rate versus a plain establishing shot.",
            "A curiosity gap gives a 4–8-year-old a reason to stay for the reveal; the reveal "
            "lands inside the same short so the question is always answered.",
            Metric.COMPLETION_RATE,
            ("hook_type",),
        ),
        _hypothesis(
            "Holding the answer until the final two seconds (a warm reveal ending) raises "
            "average watch fraction versus resolving the mystery mid-video.",
            "If the payoff arrives early there is no reason to watch the rest.",
            Metric.AVERAGE_WATCH_FRACTION,
            ("ending_type",),
        ),
        _hypothesis(
            "One continuous gentle shot holds attention better than several quick cuts for "
            "this slow, wordless character.",
            "Cuts break the feeling of discovering the hollow alongside Nib.",
            Metric.AVERAGE_WATCH_FRACTION,
            ("pace", "scene_count"),
        ),
        _hypothesis(
            "A soft music-box bed raises completion rate versus ambient forest sound alone.",
            "A simple melody signals a story with a beginning and an end.",
            Metric.COMPLETION_RATE,
            ("music_style",),
        ),
    ),
)

PUZZLE_POND = IPDefinition(
    ip=_ip(
        "puzzle-pond",
        "Puzzle Pond",
        "learning_problem_solving",
        IPProfile(
            age_range=(4, 8),
            premise=(
                "Tumble, a little otter in yellow rain boots, and Dot, a tiny clockwork "
                "dragonfly, meet one small everyday puzzle at the pond in each video and solve "
                "it by trying, noticing what went wrong, and trying a better idea."
            ),
            tone="Bright, encouraging, can-do. Mistakes are funny and useful, never shameful.",
            world_rules=(
                "Every video poses exactly one concrete problem and solves it on screen.",
                "The first idea does not work; the fix comes from noticing why.",
                "Solutions use counting, shapes, sizes, sorting, balance or simple cause and "
                "effect that a child could explain afterwards.",
                "Dot helps by pointing things out, never by solving the puzzle for Tumble.",
            ),
            visual_identity=(
                "Colourful claymation look with visible thumbprint texture, chunky shapes, "
                "bright primary colours, clear daylight, flat simple backgrounds."
            ),
            safety_constraints=(
                *_SHARED_SAFETY,
                "Water is always ankle-deep and calm; nobody swims, dives or goes under.",
            ),
            differentiation=(
                "The only IP with an explicit problem and a teachable idea in every video, "
                "and the only one with a two-character helper dynamic."
            ),
            characters=(
                CharacterProfile(
                    name="Tumble",
                    role="protagonist",
                    description="Eager and hands-on; tries things straight away, laughs when "
                    "they flop, then stops to think with a paw on the chin.",
                    appearance="Tumble is a small clay otter with warm brown fur, a pale "
                    "cream belly, round black eyes, short whiskers and bright yellow rain boots.",
                ),
                CharacterProfile(
                    name="Dot",
                    role="helper",
                    description="Observant and precise; hovers, taps the important detail "
                    "twice, and spins a happy loop when the idea works.",
                    appearance="Dot is a tiny clockwork dragonfly with a brass body, four "
                    "translucent teal wings and one round glowing blue eye.",
                ),
            ),
            suggested_handles=("puzzlepond", "puzzle.pond", "tumbleanddot"),
        ),
    ),
    initial_hypotheses=(
        _hypothesis(
            "Showing a first attempt that fails before the solution (try, fail, try again) "
            "raises completion rate versus solving the puzzle on the first try.",
            "A visible failure creates the question 'so how will it work?' that the ending "
            "answers.",
            Metric.COMPLETION_RATE,
            ("story_archetype",),
        ),
        _hypothesis(
            "Stating the problem visually in the first second (problem-first hook) raises "
            "average watch fraction versus opening on the characters arriving.",
            "Viewers decide within a second whether there is something to find out.",
            Metric.AVERAGE_WATCH_FRACTION,
            ("hook_type",),
        ),
        _hypothesis(
            "Ending with a short recap of the idea (for example counting the stones again) "
            "raises shares per view.",
            "A clear takeaway is what makes a parent pass a video on.",
            Metric.SHARES_PER_VIEW,
            ("ending_type",),
            0.15,
        ),
        _hypothesis(
            "A gentle narrator naming each step raises average watch fraction versus no narration.",
            "Naming the steps helps younger viewers follow the reasoning.",
            Metric.AVERAGE_WATCH_FRACTION,
            ("narrator_style", "dialogue_density"),
        ),
    ),
)

SOCK_PLANET = IPDefinition(
    ip=_ip(
        "sock-planet",
        "Sock Planet",
        "humor_imagination",
        IPProfile(
            age_range=(4, 8),
            premise=(
                "When nobody is looking, two odd socks, Zib and Lolo, turn a bedroom floor "
                "into anywhere they can imagine: a pillow becomes a mountain, a ribbon a "
                "river, a colander a space helmet."
            ),
            tone="Silly, bouncy, surprising. Pure visual jokes with a warm friendship at heart.",
            world_rules=(
                "Everything is make-believe built from ordinary soft household things.",
                "Each video is one imaginative transformation with a funny twist.",
                "Zib and Lolo never speak; they squeak, hum and giggle.",
                "The world is the bedroom floor and bed only: never appliances, kitchens, "
                "bathrooms, stairs, windows or outdoors.",
            ),
            visual_identity=(
                "Handmade felt and knitted stop-motion look, visible wool fibres, button "
                "eyes, saturated candy colours, soft toy-box lighting."
            ),
            safety_constraints=(
                *_SHARED_SAFETY,
                "Never show a washing machine, dryer, oven, cupboard or any place a child "
                "could climb into or get stuck in.",
                "No small objects near mouths, no strings or ribbons around necks.",
            ),
            differentiation=(
                "The only IP driven by jokes and imagination rather than a question or a "
                "problem, and the only one in a handmade textile style."
            ),
            characters=(
                CharacterProfile(
                    name="Zib",
                    role="protagonist",
                    description="The ideas sock: bold, bouncy, always first to pretend, with "
                    "a wobbling victory dance.",
                    appearance="Zib is a knitted sock puppet with red and white stripes, two "
                    "mismatched button eyes (one blue, one green) and a small stitched smile.",
                ),
                CharacterProfile(
                    name="Lolo",
                    role="best friend",
                    description="The careful sock: watches first, then makes the idea even "
                    "sillier; giggles in hiccups.",
                    appearance="Lolo is a knitted sock puppet in teal with yellow polka "
                    "dots, two round black button eyes and a tiny pom-pom on the toe.",
                ),
            ),
            suggested_handles=("sockplanet", "sock.planet", "zibandlolo"),
        ),
    ),
    initial_hypotheses=(
        _hypothesis(
            "Opening on the transformation already happening (a surprise in the first "
            "second) raises completion rate versus building up to it.",
            "Comedy shorts are judged on the first image; the setup can come after.",
            Metric.COMPLETION_RATE,
            ("hook_type",),
        ),
        _hypothesis(
            "Three quick gags at a brisk pace raise average watch fraction versus one gag "
            "played gently.",
            "A new joke every few seconds keeps resetting attention.",
            Metric.AVERAGE_WATCH_FRACTION,
            ("pace", "scene_count"),
        ),
        _hypothesis(
            "An ending that returns to the opening image (a seamless loop) raises average "
            "watch fraction.",
            "A loop invites an immediate rewatch, which counts as extra watch time.",
            Metric.AVERAGE_WATCH_FRACTION,
            ("ending_type",),
        ),
        _hypothesis(
            "A bouncy comedic music bed raises likes per view versus quiet room sound.",
            "Music tells the audience it is allowed to be funny.",
            Metric.LIKES_PER_VIEW,
            ("music_style",),
        ),
    ),
)

INITIAL_IPS: tuple[IPDefinition, ...] = (NIBBIN_HOLLOW, PUZZLE_POND, SOCK_PLANET)


def seed_initial_ips(session: Session) -> None:
    """Create the initial IPs, their characters (version 1) and starting
    hypotheses. Safe to run repeatedly."""
    for definition in INITIAL_IPS:
        ip = get_or_create_ip(session, definition.ip)
        profile = IPProfile.model_validate(definition.ip.spec)
        for character_profile in profile.characters:
            exists = session.scalars(
                select(Character).where(
                    Character.ip_id == ip.id, Character.name == character_profile.name
                )
            ).first()
            if exists is None:
                character = Character(
                    ip=ip, name=character_profile.name, role=character_profile.role
                )
                session.add(
                    CharacterVersion(
                        character=character,
                        version=1,
                        description=character_profile.description,
                        visual_spec={"appearance": character_profile.appearance},
                    )
                )
        known = set(session.scalars(select(Hypothesis.statement).where(Hypothesis.ip_id == ip.id)))
        for hypothesis in definition.initial_hypotheses:
            if hypothesis.statement not in known:
                session.add(
                    Hypothesis(
                        ip=ip,
                        statement=hypothesis.statement,
                        rationale=hypothesis.rationale,
                        prediction=hypothesis.prediction.model_dump(mode="json"),
                        source=hypothesis.source,
                    )
                )
    session.flush()
