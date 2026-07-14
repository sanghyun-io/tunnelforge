"""Persistent, fail-closed consent state for anonymous error reporting."""

from datetime import datetime, timedelta, timezone
from enum import Enum
import threading
from typing import Optional
import uuid


CONSENT_VERSION = 1
_DEFER_DAYS = 30

_STATE_SETTING = 'error_reporting_state'
_VERSION_SETTING = 'error_reporting_consent_version'
_PROMPT_COUNT_SETTING = 'error_reporting_prompt_count'
_DEFERRED_UNTIL_SETTING = 'error_reporting_deferred_until'
_INSTALLATION_ID_SETTING = 'error_reporting_installation_id'
_PROMPT_CLAIM_ID_SETTING = 'error_reporting_prompt_claim_id'
_CONSENT_GENERATION_SETTING = 'error_reporting_consent_generation'
_EMPTY_UUID_V4 = '00000000-0000-4000-8000-000000000000'
_MISSING = object()
_SUBMISSION_LEASE_LOCK = threading.RLock()


class ConsentState(str, Enum):
    UNDECIDED = 'undecided'
    DEFERRED = 'deferred'
    PROMPT_EXHAUSTED = 'prompt_exhausted'
    SUPPRESSED = 'suppressed'
    ENABLED = 'enabled'
    DISABLED_BY_USER = 'disabled_by_user'


class PromptOutcome(str, Enum):
    ENABLE = 'enable'
    ACTIVATE = 'enable'
    ENABLED = 'enable'
    LATER = 'later'
    DEFER = 'later'


class ConsentPolicy:
    """Own the local state machine for anonymous-reporting consent.

    Automatic prompting is valid only in the primary application process
    after ``main.py`` has acquired ``SingleInstanceGuard``. Consent mutations
    and report initiation share one process-local linearization lock.
    """

    def __init__(self, config_manager):
        self._config_manager = config_manager

    def should_prompt(self, now: datetime) -> bool:
        """Preview whether an automatic prompt is eligible without claiming it."""
        current_time = _as_utc(now)
        if current_time is None:
            return False
        settings = self._config_manager.get_app_settings_snapshot()
        return _is_prompt_eligible(settings, current_time)

    def claim_prompt(self, now: datetime) -> Optional[str]:
        """Atomically claim one eligible display and return its local token.

        Call only from the primary process after ``main.py`` has acquired
        ``SingleInstanceGuard``. Ineligible calls return ``None``.
        """
        current_time = _as_utc(now)
        if current_time is None:
            return None

        def claim(settings):
            if not _is_prompt_eligible(settings, current_time):
                return False, None

            state = _state(settings)
            claim_id = str(uuid.uuid4())
            is_second_exposure = (
                _version(settings) == CONSENT_VERSION
                and state == ConsentState.DEFERRED
            )
            if is_second_exposure:
                _write_consent(
                    settings,
                    ConsentState.PROMPT_EXHAUSTED,
                    2,
                    None,
                    claim_id,
                )
            else:
                _write_consent(
                    settings,
                    ConsentState.DEFERRED,
                    1,
                    _serialize_utc(
                        current_time + timedelta(days=_DEFER_DAYS)
                    ),
                    claim_id,
                )
            return True, claim_id

        with _SUBMISSION_LEASE_LOCK:
            return self._config_manager.mutate_app_settings(claim)

    def record_outcome(
        self,
        claim_id: str,
        outcome,
        now: Optional[datetime] = None,
        suppress: bool = False,
    ) -> None:
        """Consume only the current matching prompt claim and apply its outcome."""
        if type(suppress) is not bool:
            raise TypeError('suppress must be a bool')
        normalized_outcome = _outcome(outcome)
        current_time = _as_utc(now or datetime.now(timezone.utc))
        if current_time is None:
            raise ValueError('now must be a timezone-aware datetime')
        expected_claim_id = _canonical_uuid_v4(claim_id)

        def apply_outcome(settings):
            claimed_prompt = _claimed_prompt(settings, expected_claim_id)
            if claimed_prompt is None:
                return False, None
            state, prompt_count, deferred_until = claimed_prompt
            if normalized_outcome == PromptOutcome.ENABLE:
                state = ConsentState.ENABLED
                deferred_until = None
            elif suppress:
                state = ConsentState.SUPPRESSED
                deferred_until = None
            _write_consent(
                settings,
                state,
                prompt_count,
                deferred_until,
                None,
                _new_consent_generation(settings)
                if state == ConsentState.ENABLED
                else None,
            )
            return True, None

        with _SUBMISSION_LEASE_LOCK:
            self._config_manager.mutate_app_settings(apply_outcome)

    def release_prompt_claim(self, claim_id: str, now: datetime) -> None:
        """Atomically restore eligibility for the current matching claim."""
        current_time = _as_utc(now)
        if current_time is None:
            raise ValueError('now must be a timezone-aware datetime')
        expected_claim_id = _canonical_uuid_v4(claim_id)

        def release_claim(settings):
            claimed_prompt = _claimed_prompt(settings, expected_claim_id)
            if claimed_prompt is None:
                return False, None
            _, prompt_count, _ = claimed_prompt
            if prompt_count == 1:
                _write_consent(
                    settings,
                    ConsentState.UNDECIDED,
                    0,
                    None,
                    None,
                )
            else:
                _write_consent(
                    settings,
                    ConsentState.DEFERRED,
                    1,
                    _serialize_utc(current_time),
                    None,
                )
            return True, None

        with _SUBMISSION_LEASE_LOCK:
            self._config_manager.mutate_app_settings(release_claim)

    def set_enabled(self, enabled: bool) -> None:
        """Apply an explicit Settings enable or disable choice atomically."""
        if type(enabled) is not bool:
            raise TypeError('enabled must be a bool')
        target_state = (
            ConsentState.ENABLED if enabled else ConsentState.DISABLED_BY_USER
        )

        def apply_setting(settings):
            prompt_count = _current_prompt_count(settings)
            _write_consent(
                settings,
                target_state,
                prompt_count if prompt_count is not None else 0,
                None,
                None,
                _new_consent_generation(settings) if enabled else None,
            )
            return True, None

        with _SUBMISSION_LEASE_LOCK:
            self._config_manager.mutate_app_settings(apply_setting)

    def is_enabled(self) -> bool:
        """Return true only for affirmative consent at the current version."""
        settings = self._config_manager.get_app_settings_snapshot()
        return _is_affirmatively_enabled(settings)

    def capture_submission_token(self) -> Optional[str]:
        """Capture the generation that authorizes one pending report worker."""

        def capture(settings):
            if not _is_affirmatively_enabled(settings):
                return False, None
            generation = _consent_generation(settings)
            if generation is not None:
                return False, generation
            generation = _new_consent_generation(settings)
            settings[_CONSENT_GENERATION_SETTING] = generation
            return True, generation

        with _SUBMISSION_LEASE_LOCK:
            return self._config_manager.mutate_app_settings(capture)

    def is_submission_token_current(self, token: object) -> bool:
        """Validate an exact captured generation against one coherent snapshot."""
        expected = _canonical_uuid_v4(token)
        if expected is None:
            return False
        with _SUBMISSION_LEASE_LOCK:
            settings = self._config_manager.get_app_settings_snapshot()
            return (
                _is_affirmatively_enabled(settings)
                and _consent_generation(settings) == expected
            )

    def authorize_submission(self, token: object) -> bool:
        """Linearize and commit one dispatch against the consent generation.

        Revocation that linearizes first prevents the permit. A true result is
        the dispatch commit point, so later revocation does not recall it even
        before Python enters Session.post. The shared lock is released before
        blocking network I/O so Settings disable remains responsive.
        """
        expected = _canonical_uuid_v4(token)
        with _SUBMISSION_LEASE_LOCK:
            settings = self._config_manager.get_app_settings_snapshot()
            return (
                expected is not None
                and _is_affirmatively_enabled(settings)
                and _consent_generation(settings) == expected
            )


def _is_affirmatively_enabled(settings):
    return (
        _state(settings) == ConsentState.ENABLED
        and _version(settings) == CONSENT_VERSION
        and _current_prompt_count(settings) is not None
        and settings.get(_DEFERRED_UNTIL_SETTING) is None
        and settings.get(_PROMPT_CLAIM_ID_SETTING) is None
    )


def _is_prompt_eligible(settings, current_time):
    state = _state(settings)
    if state is None:
        return False
    if state in (ConsentState.SUPPRESSED, ConsentState.DISABLED_BY_USER):
        return False

    raw_version = settings.get(_VERSION_SETTING, _MISSING)
    if raw_version is not _MISSING and type(raw_version) is not int:
        return False
    version = None if raw_version is _MISSING else raw_version
    if version is not None and (version < 0 or version > CONSENT_VERSION):
        return False

    if state == ConsentState.UNDECIDED:
        raw_count = settings.get(_PROMPT_COUNT_SETTING, _MISSING)
        raw_deferred_until = settings.get(_DEFERRED_UNTIL_SETTING, _MISSING)
        if _STATE_SETTING not in settings:
            return (
                raw_version is _MISSING
                and raw_count is _MISSING
                and raw_deferred_until is _MISSING
            )
        return (
            version in (None, CONSENT_VERSION - 1, CONSENT_VERSION)
            and (
                raw_count is _MISSING
                or (type(raw_count) is int and raw_count == 0)
            )
            and raw_deferred_until in (_MISSING, None)
        )

    if version is None:
        return False

    if version < CONSENT_VERSION:
        if state == ConsentState.ENABLED:
            return (
                _optional_prompt_count(settings) is not None
                and settings.get(_DEFERRED_UNTIL_SETTING) is None
            )
        if state == ConsentState.PROMPT_EXHAUSTED:
            return (
                _prompt_count(settings) == 2
                and settings.get(_DEFERRED_UNTIL_SETTING) is None
            )
        if state == ConsentState.DEFERRED:
            return _deferred_is_due(settings, current_time)
        return False

    if state == ConsentState.ENABLED:
        return False
    if state == ConsentState.PROMPT_EXHAUSTED:
        return False
    if state == ConsentState.DEFERRED:
        return _deferred_is_due(settings, current_time)
    return False


def _deferred_is_due(settings, current_time):
    if _prompt_count(settings) != 1:
        return False
    deferred_until = _parse_utc(settings.get(_DEFERRED_UNTIL_SETTING))
    return deferred_until is not None and current_time >= deferred_until


def _claimed_prompt(settings, expected_claim_id):
    if expected_claim_id is None:
        return None
    if _version(settings) != CONSENT_VERSION:
        return None
    if _claim_id(settings) != expected_claim_id:
        return None
    state = _state(settings)
    prompt_count = _prompt_count(settings)
    deferred_until = settings.get(_DEFERRED_UNTIL_SETTING)
    if (
        state == ConsentState.DEFERRED
        and prompt_count == 1
        and _parse_utc(deferred_until) is not None
    ):
        return state, 1, deferred_until
    if (
        state == ConsentState.PROMPT_EXHAUSTED
        and prompt_count == 2
        and deferred_until is None
    ):
        return state, 2, None
    return None


def _current_prompt_count(settings):
    if _version(settings) != CONSENT_VERSION:
        return None
    state = _state(settings)
    prompt_count = _prompt_count(settings)
    if state == ConsentState.UNDECIDED:
        return 0 if settings.get(_PROMPT_COUNT_SETTING) in (None, 0) else None
    if state == ConsentState.DEFERRED:
        return 1 if prompt_count == 1 else None
    if state == ConsentState.PROMPT_EXHAUSTED:
        return 2 if prompt_count == 2 else None
    if state in (
        ConsentState.ENABLED,
        ConsentState.SUPPRESSED,
        ConsentState.DISABLED_BY_USER,
    ):
        return prompt_count
    return None


def _state(settings):
    raw_state = settings.get(_STATE_SETTING, _MISSING)
    if raw_state is _MISSING:
        return ConsentState.UNDECIDED
    if not isinstance(raw_state, str):
        return None
    try:
        return ConsentState(raw_state)
    except ValueError:
        return None


def _version(settings):
    value = settings.get(_VERSION_SETTING)
    return value if type(value) is int else None


def _prompt_count(settings):
    value = settings.get(_PROMPT_COUNT_SETTING)
    if type(value) is not int or value < 0 or value > 2:
        return None
    return value


def _optional_prompt_count(settings):
    value = settings.get(_PROMPT_COUNT_SETTING)
    if value is None:
        return 0
    return _prompt_count(settings)


def _write_consent(
    settings,
    state,
    prompt_count,
    deferred_until,
    claim_id,
    consent_generation=None,
):
    settings.update({
        _STATE_SETTING: state.value,
        _VERSION_SETTING: CONSENT_VERSION,
        _PROMPT_COUNT_SETTING: prompt_count,
        _DEFERRED_UNTIL_SETTING: deferred_until,
        _INSTALLATION_ID_SETTING: _installation_id(settings),
        _PROMPT_CLAIM_ID_SETTING: claim_id,
        _CONSENT_GENERATION_SETTING: consent_generation,
    })


def _installation_id(settings):
    value = _canonical_uuid_v4(settings.get(_INSTALLATION_ID_SETTING))
    if value is not None:
        return value
    return str(uuid.uuid4())


def _claim_id(settings):
    return _canonical_uuid_v4(settings.get(_PROMPT_CLAIM_ID_SETTING))


def _consent_generation(settings):
    generation = _canonical_uuid_v4(settings.get(_CONSENT_GENERATION_SETTING))
    installation_id = _canonical_uuid_v4(settings.get(_INSTALLATION_ID_SETTING))
    if (
        generation is None
        or installation_id is None
        or generation == installation_id
    ):
        return None
    return generation


def _new_consent_generation(settings):
    installation_id = _installation_id(settings)
    settings[_INSTALLATION_ID_SETTING] = installation_id
    for _ in range(2):
        generation = str(uuid.uuid4())
        if generation != installation_id:
            return generation
    raise RuntimeError('Unable to create consent generation')


def _canonical_uuid_v4(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    if (
        parsed.version == 4
        and parsed.variant == uuid.RFC_4122
        and str(parsed) == value
        and value != _EMPTY_UUID_V4
    ):
        return value
    return None


def _outcome(value):
    if isinstance(value, PromptOutcome):
        return value
    try:
        return PromptOutcome(value)
    except (TypeError, ValueError):
        raise ValueError('outcome must be a PromptOutcome')


def _as_utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _parse_utc(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _as_utc(parsed)


def _serialize_utc(value):
    return _as_utc(value).isoformat()
