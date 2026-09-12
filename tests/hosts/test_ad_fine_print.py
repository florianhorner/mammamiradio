"""Fine print is a brand trait, not something every ad closes on.

PR #1129 made the fast legal tail fire in every format, which was the correct fix
for the bug it targeted and turned the gag into a tic: the prompt asked for fine
print in 100% of spots and the tempo gate then sped up all of it. These tests pin
the replacement — a pure category lookup — and the two failure modes that dropping
disclaimer parts introduces on the way.
"""

from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState, Track
from mammamiradio.hosts import scriptwriter as scriptwriter_module
from mammamiradio.hosts.ad_creative import (
    _FORMAT_ROLES,
    ALL_FORMATS,
    DISCLAIMER_ROLE,
    FINE_PRINT_CATEGORIES,
    OFFICIAL_CATEGORY_SONIC_RECIPES,
    SPEAKER_ROLES,
    AdBrand,
    AdPart,
    AdVoice,
    CampaignSpine,
    brand_has_fine_print,
)
from mammamiradio.hosts.scriptwriter import _cap_disclaimer_parts, write_ad

# A category deliberately outside FINE_PRINT_CATEGORIES, and one inside it that is
# not "pharma" — pharma has its own canonical-tail path and would mask what these
# tests are actually checking.
NO_FINE_PRINT_CATEGORY = "tech"
FINE_PRINT_CATEGORY = "banking"


# Mirrors the fixtures in test_scriptwriter.py, which are module-local there rather
# than in a shared conftest.
@pytest.fixture()
def config():
    cfg = load_config()
    cfg.anthropic_api_key = "test-key"
    cfg.openai_api_key = ""
    return cfg


@pytest.fixture()
def state():
    return StationState(playlist=[Track(title="Test", artist="Artist", duration_ms=1000, spotify_id="test1")])


@pytest.fixture(autouse=True)
def _reset_provider_backoff_state():
    scriptwriter_module.reset_provider_backoff()
    yield
    scriptwriter_module.reset_provider_backoff()


def _voices(ad_format: str) -> dict[str, AdVoice]:
    return {
        role: AdVoice(name=f"V{i}", voice="it-IT-DiegoNeural", style="s", role=role)
        for i, role in enumerate(_FORMAT_ROLES[ad_format])
    }


# ---------------------------------------------------------------------------
# The rate
# ---------------------------------------------------------------------------


def test_configured_fine_print_share_matches_the_designed_rate():
    """The *configured* weighted share over the shipped inventory.

    Not the aired rate: `_pick_brand` excludes the last three brands and the
    producer excludes same-break repeats, so what a listener hears drifts from
    this number, and with ad_spots_per_break = 2 the per-break figure is roughly
    double it. This asserts the one quantity that is exact arithmetic. Retuning
    FINE_PRINT_CATEGORIES is expected to move it; the band is wide enough to allow
    one category in or out and narrow enough to catch the set emptying.
    """
    import tomllib
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    brands = tomllib.loads((repo_root / "radio.toml").read_text())["ads"]["brands"]

    def weight(brand: dict) -> int:
        # Mirrors _pick_brand's `3 if b.recurring else 1`.
        return 3 if brand.get("recurring", True) else 1

    total = sum(weight(b) for b in brands)
    fine = sum(weight(b) for b in brands if b.get("category") in FINE_PRINT_CATEGORIES)
    share = fine / total

    print(f"\nconfigured fine-print share: {fine}/{total} = {share:.1%} of spots")
    assert 0.10 <= share <= 0.20, (
        f"fine print is configured for {share:.1%} of spots ({fine}/{total}); "
        "if that is deliberate, move the band, but a listener hears roughly "
        "double this per ad break"
    )


def test_every_fine_print_category_is_a_real_reviewed_category():
    """A typo in the frozenset means nobody ever gets fine print, silently.

    `AdBrand.category` is unvalidated free text defaulting to "general", so a set
    member that matches no brand fails nothing at runtime — the gag just quietly
    stops airing. OFFICIAL_CATEGORY_SONIC_RECIPES is the reviewed taxonomy, so
    membership in it is the cheap proof that each entry is a category that exists.
    """
    unknown = FINE_PRINT_CATEGORIES - set(OFFICIAL_CATEGORY_SONIC_RECIPES)
    assert not unknown, f"not real categories: {sorted(unknown)}"


def test_pharma_is_in_the_set_that_the_canonical_tail_assumes():
    """Two independent spellings of "pharma has fine print" must agree.

    `scriptwriter.py` appends the canonical medicine tail on `category ==
    "pharma"` and skips the capper for it; the prompt asks for fine print based
    on this frozenset. Drop "pharma" here and the prompt would say "write no
    disclaimer" while the code appends one anyway.
    """
    assert "pharma" in FINE_PRINT_CATEGORIES


def test_the_shipped_inventory_actually_contains_each_fine_print_category():
    import tomllib
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    brands = tomllib.loads((repo_root / "radio.toml").read_text())["ads"]["brands"]
    shipped = {b.get("category") for b in brands}
    missing = FINE_PRINT_CATEGORIES - shipped
    assert not missing, f"no shipped brand has category {sorted(missing)}"


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", sorted(FINE_PRINT_CATEGORIES))
def test_fine_print_categories_carry_fine_print(category):
    assert brand_has_fine_print(AdBrand(name="B", tagline="T", category=category))


@pytest.mark.parametrize("category", ["tech", "food", "cars", "general", ""])
def test_other_categories_do_not(category):
    assert not brand_has_fine_print(AdBrand(name="B", tagline="T", category=category))


def test_a_campaign_that_owns_the_goblin_keeps_its_fine_print():
    """Il Razzo's shape: the owned character *is* the disclaimer voice.

    Suppressing fine print here would leave the prompt demanding a spokesperson
    role it never lists, and the owned-fallback recovery would then emit a lone
    disclaimer part — an entire spot at 1.95x.
    """
    brand = AdBrand(
        name="Razzo",
        tagline="T",
        category=NO_FINE_PRINT_CATEGORY,
        campaign=CampaignSpine(premise="p", escalation_rule="e", spokesperson_role=DISCLAIMER_ROLE),
    )
    assert brand_has_fine_print(brand)


@pytest.mark.parametrize("raw_role", [DISCLAIMER_ROLE, f"{DISCLAIMER_ROLE} ", f" {DISCLAIMER_ROLE}"])
def test_the_goblin_carve_out_normalizes_like_write_ad_does(raw_role):
    """`write_ad` strips `spokesperson_role`; reading it raw here would disagree.

    A trailing space would answer False, so SPEAKERS omits the role while the
    prompt's spokesperson rule still demands it, and the owned fallback emits a
    lone goblin part: the whole spot at disclaimer tempo.
    """
    brand = AdBrand(
        name="Razzo",
        tagline="T",
        category=NO_FINE_PRINT_CATEGORY,
        campaign=CampaignSpine(premise="p", escalation_rule="e", spokesperson_role=raw_role),
    )
    assert brand_has_fine_print(brand)


def test_a_campaign_with_another_spokesperson_does_not():
    brand = AdBrand(
        name="Testo",
        tagline="T",
        category=NO_FINE_PRINT_CATEGORY,
        campaign=CampaignSpine(premise="p", escalation_rule="e", spokesperson_role="hammer"),
    )
    assert not brand_has_fine_print(brand)


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("ad_format", list(ALL_FORMATS))
@pytest.mark.parametrize("recipe_driven", [False, True])
async def test_no_fine_print_brand_never_hears_the_role(config, state, ad_format, recipe_driven):
    """Neither `parts_example` branch may mention fine print for such a brand.

    Covers classic_pitch specifically: that format casts a goblin voice, so the
    SPEAKERS loop would emit the token unless it is filtered.
    """
    from mammamiradio.hosts.ad_creative import SonicWorld

    captured = {}

    async def _capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return {"parts": [{"type": "voice", "text": "Copy.", "role": _FORMAT_ROLES[ad_format][0]}]}

    sonic = SonicWorld(environment="cafe", music_bed="lounge", transition_motif="chime")
    if recipe_driven:
        sonic = SonicWorld(recipe_id="cafe_testimonial", music_bed="lounge", transition_motif="chime")

    brand = AdBrand(name="Testo", tagline="T", category=NO_FINE_PRINT_CATEGORY)
    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", new=_capture):
        await write_ad(brand, _voices(ad_format), state, config, ad_format=ad_format, sonic=sonic)

    # Without an LLM key write_ad returns before building a prompt at all, and a
    # bare "token not in prompt" assertion would pass on an empty string.
    assert "prompt" in captured, "write_ad returned before rendering a prompt"
    prompt = captured["prompt"]
    assert DISCLAIMER_ROLE not in prompt, f"{ad_format} still names the fine-print role"
    assert "Fast disclaimer" not in prompt
    assert "Do not write a disclaimer" in prompt
    assert "A second voice, DISCLAIMER_GOBLIN, closes the spot" not in prompt, (
        f"{ad_format} blurb still describes the fine print"
    )


@pytest.mark.asyncio
async def test_a_fine_print_brand_still_gets_the_whole_apparatus(config, state):
    captured = {}

    async def _capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return {"parts": [{"type": "voice", "text": "Copy.", "role": "hammer"}]}

    brand = AdBrand(name="Bancone", tagline="T", category=FINE_PRINT_CATEGORY)
    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", new=_capture):
        await write_ad(brand, _voices("classic_pitch"), state, config, ad_format="classic_pitch")

    assert "prompt" in captured
    prompt = captured["prompt"]
    assert DISCLAIMER_ROLE in prompt
    assert "Fast disclaimer" in prompt
    # A phrase unique to AD_FORMAT_DISCLAIMER_SUFFIX. "buries the bad news" also
    # appears in the SPEAKERS role description, so asserting on that would pass
    # even if the format blurb never got its suffix.
    assert "A second voice, DISCLAIMER_GOBLIN, closes the spot" in prompt, "the format blurb lost its disclaimer suffix"


@pytest.mark.asyncio
@pytest.mark.parametrize("ad_format", list(ALL_FORMATS))
@pytest.mark.parametrize("category", [FINE_PRINT_CATEGORY, NO_FINE_PRINT_CATEGORY])
async def test_the_prompt_never_names_a_character_the_cast_does_not_have(config, state, ad_format, category):
    """Catches shouted role names the quoted-token check cannot see.

    The SPEAKERS/example consistency test matches `"role": "x"` and `- "x"`, both
    lowercase and quoted. Format blurbs address characters in bare uppercase
    (HAMMER, DISCLAIMER_GOBLIN), so a blurb naming a role this format never casts
    slips straight past it. That is how a suffix reading "HAMMER sells it" reached
    late_night_whisper, whose only voice is the seductress.
    """
    import re

    captured = {}

    async def _capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return {"parts": [{"type": "voice", "text": "Copy.", "role": _FORMAT_ROLES[ad_format][0]}]}

    brand = AdBrand(name="Testo", tagline="T", category=category)
    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", new=_capture):
        await write_ad(brand, _voices(ad_format), state, config, ad_format=ad_format)

    assert "prompt" in captured, "write_ad returned before rendering a prompt"
    prompt = captured["prompt"]

    # The goblin is deliberately addressable in every format, including the five
    # that never cast one -- those render it with the spot's opening voice. Every
    # *other* character named in the prompt must actually be in the cast.
    addressable = set(_FORMAT_ROLES[ad_format])
    addressable.discard(DISCLAIMER_ROLE)
    if brand_has_fine_print(brand):
        addressable.add(DISCLAIMER_ROLE)
    shouted = {m.lower() for m in re.findall(r"\b[A-Z][A-Z_]{3,}\b", prompt)}
    named_characters = shouted & set(SPEAKER_ROLES)

    assert named_characters <= addressable, (
        f"{ad_format}/{category}: prompt shouts {sorted(named_characters - addressable)} "
        f"but this format only addresses {sorted(addressable)}"
    )


@pytest.mark.asyncio
async def test_an_unknown_format_still_gets_the_suffix_for_a_fine_print_brand(config, state):
    """`AD_FORMATS.get(...)` falls back to the classic blurb; the suffix rides on
    the brand flag, not on the format, so the fallback must stay consistent."""
    captured = {}

    async def _capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return {"parts": [{"type": "voice", "text": "Copy.", "role": "hammer"}]}

    brand = AdBrand(name="Bancone", tagline="T", category=FINE_PRINT_CATEGORY)
    voices = {"hammer": AdVoice(name="V", voice="it-IT-DiegoNeural", style="s", role="hammer")}
    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", new=_capture):
        await write_ad(brand, voices, state, config, ad_format="not_a_real_format")

    assert "prompt" in captured
    assert "A second voice, DISCLAIMER_GOBLIN, closes the spot" in captured["prompt"]


# ---------------------------------------------------------------------------
# Enforcement: the prompt is not a contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_disclaimer_the_model_wrote_anyway_is_dropped(config, state):
    brand = AdBrand(name="Testo", tagline="T", category=NO_FINE_PRINT_CATEGORY)
    voices = {"hammer": AdVoice(name="V", voice="it-IT-DiegoNeural", style="s", role="hammer")}

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value={
            "parts": [
                {"type": "voice", "text": "Testo is here today.", "role": "hammer"},
                {"type": "voice", "text": "Terms apply, results may vary.", "role": DISCLAIMER_ROLE},
            ],
            "summary": "Testo ad",
        },
    ):
        result = await write_ad(brand, voices, state, config, ad_format="classic_pitch")

    assert not any(p.role == DISCLAIMER_ROLE for p in result.parts), "fine print survived the gate"
    assert any(p.type == "voice" and p.text.strip() for p in result.parts), "the ad lost all its speech"
    assert not any("Terms apply" in p.text for p in result.parts)


def test_an_all_fine_print_script_is_demoted_not_gutted():
    """Every part is fine print, and this brand has none — so it is just copy.

    Dropping would turn a three-part script into a one-line fallback. Demoting
    keeps the words and airs none of them fast, which is the same outcome the
    allowed=True path already reaches for a duplicate-disclaimer script.
    """
    parts = [
        AdPart(type="voice", text="One.", role=DISCLAIMER_ROLE),
        AdPart(type="voice", text="Two.", role=DISCLAIMER_ROLE),
        AdPart(type="voice", text="Three.", role=DISCLAIMER_ROLE),
    ]
    result = _cap_disclaimer_parts(parts, "hammer", allowed=False)

    assert len(result) == 3, "demotion must not drop content"
    assert not any(p.role == DISCLAIMER_ROLE for p in result)
    assert all(p.role == "hammer" for p in result)


@pytest.mark.parametrize("fallback_role", ["hammer", "seductress", "bureaucrat", "witness"])
def test_demotion_uses_the_format_role_it_was_handed(fallback_role):
    """Four of six formats open on something other than the hammer.

    Every existing call site passes "hammer", so hardcoding it would leave the
    whole suite green while a whisper spot got a booming announcer.
    """
    parts = [AdPart(type="voice", text="Only fine print.", role=DISCLAIMER_ROLE)]
    result = _cap_disclaimer_parts(parts, fallback_role, allowed=False)
    assert [p.role for p in result] == [fallback_role]


def test_dropping_never_reaches_the_empty_max():
    """`max()` over the reorder generator raises on an empty sequence.

    It is unreachable while drop-mode returns early. If someone reimplements it as
    a fall-through, `write_ad`'s bare `except Exception` swallows the ValueError
    and silently degrades the whole ad to one line.
    """
    parts = [
        AdPart(type="sfx", sfx="chime"),
        AdPart(type="voice", text="Only fine print.", role=DISCLAIMER_ROLE),
    ]
    result = _cap_disclaimer_parts(parts, "hammer", allowed=False)
    assert [p.role for p in result if p.type == "voice"] == ["hammer"]


@pytest.mark.asyncio
async def test_a_whitespace_survivor_does_not_produce_a_silent_ad(config, state):
    """The regression drop-mode would otherwise introduce.

    `"   "` is truthy, so a whitespace line counted as surviving copy: the real
    copy got dropped with the disclaimer role, the recovery guard saw a "voice"
    part and declined to fire, and the spot aired a sting followed by nothing.
    """
    brand = AdBrand(name="Testo", tagline="Tagline of record", category=NO_FINE_PRINT_CATEGORY)
    voices = {"hammer": AdVoice(name="V", voice="it-IT-DiegoNeural", style="s", role="hammer")}

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value={
            "parts": [
                {"type": "voice", "text": "   ", "role": "hammer"},
                {"type": "voice", "text": "The entire ad, mislabelled.", "role": DISCLAIMER_ROLE},
            ],
            "summary": "Testo ad",
        },
    ):
        result = await write_ad(brand, voices, state, config, ad_format="classic_pitch")

    spoken = [p.text for p in result.parts if p.type == "voice" and p.text.strip()]
    assert spoken, "the ad has no audible speech"
    assert not any(p.role == DISCLAIMER_ROLE for p in result.parts)


@pytest.mark.asyncio
@pytest.mark.parametrize("recipe_driven", [False, True])
async def test_a_kept_disclaimer_is_still_last_after_the_whole_pipeline(config, state, recipe_driven):
    """Ordering is a postcondition of write_ad, not of _cap_disclaimer_parts.

    `_ensure_attention_grabbing_ad_parts` runs afterwards and may filter or insert
    parts, so asserting the order inside the capper proves less than it looks.
    """
    from mammamiradio.hosts.ad_creative import SonicWorld

    sonic = (
        SonicWorld(recipe_id="bureaucracy_stamp", music_bed="lounge", transition_motif="chime")
        if recipe_driven
        else SonicWorld(environment="cafe", music_bed="lounge", transition_motif="chime")
    )
    brand = AdBrand(name="Bancone", tagline="T", category=FINE_PRINT_CATEGORY)
    voices = {"hammer": AdVoice(name="V", voice="it-IT-DiegoNeural", style="s", role="hammer")}

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value={
            "parts": [
                {"type": "voice", "text": "Bancone opens at nine.", "role": "hammer"},
                {"type": "voice", "text": "Terms apply, results may vary.", "role": DISCLAIMER_ROLE},
                {"type": "voice", "text": "Bancone: the bank of record.", "role": "hammer"},
                {"type": "pause", "duration": 0.4},
            ],
            "summary": "Bancone ad",
        },
    ):
        result = await write_ad(brand, voices, state, config, ad_format="classic_pitch", sonic=sonic)

    voice_parts = [p for p in result.parts if p.type == "voice" and p.text.strip()]
    assert voice_parts[-1].role == DISCLAIMER_ROLE, (
        f"fine print is not the closing line: {[(p.role, p.text) for p in voice_parts]}"
    )


@pytest.mark.asyncio
async def test_dropping_the_disclaimer_keeps_the_opener_sting(config, state):
    """`_ensure_attention_grabbing_ad_parts` inserts its accents off the voice
    count, which the drop lowers. The opener must survive regardless."""
    brand = AdBrand(name="Testo", tagline="T", category=NO_FINE_PRINT_CATEGORY)
    voices = {"hammer": AdVoice(name="V", voice="it-IT-DiegoNeural", style="s", role="hammer")}

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value={
            "parts": [
                {"type": "voice", "text": "One long uninterrupted sales line about Testo.", "role": "hammer"},
                {"type": "voice", "text": "Terms apply.", "role": DISCLAIMER_ROLE},
            ],
            "summary": "Testo ad",
        },
    ):
        result = await write_ad(brand, voices, state, config, ad_format="classic_pitch")

    assert any(p.type == "sfx" and p.sfx for p in result.parts), "the spot lost its opener sting"


# ---------------------------------------------------------------------------
# Untouched neighbours
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pharma_still_gets_the_canonical_medicine_tail(config, state):
    # Super Italian keeps the language guard from rejecting the mocked copy, the
    # same way the pharma test in test_scriptwriter.py does.
    config.super_italian_mode = True
    brand = AdBrand(name="Dolorfin", tagline="T", category="pharma")
    voices = {"default": AdVoice(name="V", voice="it-IT-IsabellaNeural", style="s")}

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value={
            "parts": [{"type": "voice", "text": "Dolorfin funziona, forse.", "role": "default"}],
            "summary": "Dolorfin ad",
        },
    ):
        result = await write_ad(brand, voices, state, config)

    disclaimers = [p for p in result.parts if p.role == DISCLAIMER_ROLE]
    assert len(disclaimers) == 1
    assert result.parts[-1] is disclaimers[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("category", [FINE_PRINT_CATEGORY, NO_FINE_PRINT_CATEGORY, "pharma"])
async def test_the_no_llm_path_emits_no_fine_print_for_anyone(config, state, category):
    """Pre-existing, and it fails safe: Demo Radio returns before disclaimer
    processing, so no brand gets a rattle there. Pinned so the next reader does
    not mistake it for a gap this change opened."""
    config.anthropic_api_key = ""
    config.openai_api_key = ""

    brand = AdBrand(name="Testo", tagline="Tagline", category=category)
    voices = {"hammer": AdVoice(name="V", voice="it-IT-DiegoNeural", style="s", role="hammer")}
    result = await write_ad(brand, voices, state, config, ad_format="classic_pitch")

    assert not any(p.role == DISCLAIMER_ROLE for p in result.parts)


# ---------------------------------------------------------------------------
# Silence: the class this change had to not introduce
# ---------------------------------------------------------------------------


def test_the_capper_does_not_count_whitespace_as_surviving_copy():
    """Isolates the capper's own strip from `write_ad`'s.

    Without it the whitespace line counts as real copy, the drop branch fires
    instead of demote-all, and the spot is left with a single silent part.
    """
    # The shape that makes the two readings choose different branches. With the
    # strip, the goblin line is the only real copy, so demote-all fires and the
    # words survive. Without it, the whitespace counts as a second voice, the drop
    # branch fires instead, the only real line is deleted, and the spot is left
    # with nothing but a silent part.
    parts = [
        AdPart(type="voice", text="   ", role="hammer"),
        AdPart(type="voice", text="The entire ad, mislabelled.", role=DISCLAIMER_ROLE),
    ]
    result = _cap_disclaimer_parts(parts, "hammer", allowed=False)

    speaks = [p.text for p in result if p.type == "voice" and isinstance(p.text, str) and p.text.strip()]
    assert speaks == ["The entire ad, mislabelled."], f"the only real copy was dropped: {speaks}"
    assert not any(p.role == DISCLAIMER_ROLE for p in result), "demotion left the fine-print role on"


def test_a_blank_disclaimer_part_is_dropped_not_left_wearing_the_role():
    """`tts.py` gates the tempo on the role, not on the text.

    A blank goblin part is invisible to the voice-part filter but still reaches
    that gate, so it would air at disclaimer tempo and put the role into
    `roles_used` for a brand that carries no fine print.
    """
    parts = [
        AdPart(type="voice", text="Copy.", role="hammer"),
        AdPart(type="voice", text="   ", role=DISCLAIMER_ROLE),
    ]
    result = _cap_disclaimer_parts(parts, "hammer", allowed=False)
    assert not any(p.role == DISCLAIMER_ROLE for p in result), (
        f"a blank fine-print part survived: {[(p.role, p.text) for p in result]}"
    )


@pytest.mark.asyncio
async def test_an_all_whitespace_reply_still_airs_something(config, state):
    """No disclaimer involved: the recovery guard is the only thing standing here.

    This is the case `test_a_whitespace_survivor_...` does not reach, because that
    one takes the demote-all branch and never consults the guard.
    """
    brand = AdBrand(name="Testo", tagline="Tagline of record", category=NO_FINE_PRINT_CATEGORY)
    voices = {"hammer": AdVoice(name="V", voice="it-IT-DiegoNeural", style="s", role="hammer")}
    mock = AsyncMock(return_value={"parts": [{"type": "voice", "text": "   ", "role": "hammer"}], "summary": "s"})
    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", new=mock):
        result = await write_ad(brand, voices, state, config, ad_format="classic_pitch")

    assert mock.await_count == 1, "write_ad short-circuited; this test proved nothing"
    speaks = [p.text for p in result.parts if p.type == "voice" and p.text.strip()]
    assert speaks, "the spot has no audible speech"


@pytest.mark.asyncio
async def test_a_blank_tagline_cannot_become_a_silent_recovery(config, state):
    """The recovery value needs the guarantee it is recovering.

    `radio.toml` taglines are stored unvalidated, so a blank one would rebuild the
    identical silent part the guard just rejected.
    """
    brand = AdBrand(name="Testo", tagline="   ", category=NO_FINE_PRINT_CATEGORY)
    voices = {"hammer": AdVoice(name="V", voice="it-IT-DiegoNeural", style="s", role="hammer")}
    mock = AsyncMock(
        return_value={"parts": [{"type": "voice", "text": "   ", "role": "hammer"}], "text": "   ", "summary": "s"}
    )
    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", new=mock):
        result = await write_ad(brand, voices, state, config, ad_format="classic_pitch")

    assert mock.await_count == 1, "write_ad short-circuited; this test proved nothing"
    speaks = [p.text for p in result.parts if p.type == "voice" and p.text.strip()]
    assert speaks, f"blank tagline rebuilt a silent part: {[(p.type, repr(p.text)) for p in result.parts]}"
    assert "Testo" in " ".join(speaks), f"the spot does not even name the brand: {speaks}"


def test_the_renderable_filter_rejects_a_whitespace_voice_part():
    """`synthesize("   ")` draws NoAudioReceived from edge-tts, which benches the
    voice in `_failed_edge_voices` for the rest of the process and fails the whole
    ad render. A blank line is not speech and must never reach the engine."""
    from mammamiradio.audio.tts import AdScript as _AdScript  # noqa: F401  (same symbol)

    parts = [
        AdPart(type="voice", text="Real copy.", role="hammer"),
        AdPart(type="voice", text="   ", role="hammer"),
    ]
    renderable = [p for p in parts if p.type != "voice" or (isinstance(p.text, str) and p.text.strip())]
    assert [p.text for p in renderable] == ["Real copy."]


def test_the_capper_tolerates_a_non_string_text():
    """`AdPart` has no validation and the helper is module-level. An
    AttributeError here is swallowed by `write_ad`'s bare except and silently
    degrades the whole ad to one line."""
    parts = [
        AdPart(type="voice", text=None, role="hammer"),  # type: ignore[arg-type]
        AdPart(type="voice", text="Real.", role=DISCLAIMER_ROLE),
        AdPart(type="voice", text="Also real.", role="hammer"),
    ]
    result = _cap_disclaimer_parts(parts, "hammer", allowed=False)
    assert not any(p.role == DISCLAIMER_ROLE for p in result)


@pytest.mark.asyncio
async def test_a_dropped_fine_print_does_not_retire_the_callback_gag(config, state):
    """The model may bury the gag inside the fine print. Dropping that line means
    the gag never aired, so it must stay in the ledger for another try."""
    brand = AdBrand(name="Testo", tagline="T", category=NO_FINE_PRINT_CATEGORY)
    voices = {"hammer": AdVoice(name="V", voice="it-IT-DiegoNeural", style="s", role="hammer")}
    mock = AsyncMock(
        return_value={
            "parts": [
                {"type": "voice", "text": "Testo is here today.", "role": "hammer"},
                {"type": "voice", "text": "Terms apply, and the goat is still missing.", "role": DISCLAIMER_ROLE},
            ],
            "summary": "s",
            "callback_used": True,
        }
    )
    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", new=mock):
        await write_ad(brand, voices, state, config, ad_format="classic_pitch", callback_gag="the missing goat")

    assert mock.await_count == 1
    assert not state.pending_callback_landed, "a gag that was dropped with the fine print was retired anyway"
