import copy
from datetime import datetime, timedelta, timezone
import json
import threading
import uuid

import pytest

from src.core.error_report_consent import (
    CONSENT_VERSION,
    ConsentPolicy,
    ConsentState,
    PromptOutcome,
)
from src.core.error_report_builder import build_error_report


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
INSTALLATION_ID = '550e8400-e29b-41d4-a716-446655440000'
CLAIM_A = '11111111-1111-4111-8111-111111111111'
CLAIM_B = '22222222-2222-4222-8222-222222222222'
GENERATION_A = '33333333-3333-4333-8333-333333333333'
GENERATION_B = '44444444-4444-4444-8444-444444444444'
CONSENT_KEYS = {
    'error_reporting_state',
    'error_reporting_consent_version',
    'error_reporting_prompt_count',
    'error_reporting_deferred_until',
    'error_reporting_installation_id',
    'error_reporting_prompt_claim_id',
    'error_reporting_consent_generation',
}


class MemoryConfigManager:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.update_calls = []
        self._lock = threading.RLock()

    def get_app_setting(self, key, default=None):
        with self._lock:
            return self.settings.get(key, default)

    def get_app_settings_snapshot(self):
        with self._lock:
            return copy.deepcopy(self.settings)

    def mutate_app_settings(self, mutator):
        with self._lock:
            snapshot = copy.deepcopy(self.settings)
            should_save, result = mutator(snapshot)
            if type(should_save) is not bool:
                raise TypeError('should_save must be a bool')
            if should_save:
                self.update_calls.append(copy.deepcopy(snapshot))
                self.settings = copy.deepcopy(snapshot)
            return result

    def set_app_settings(self, updates):
        copied_updates = dict(updates)

        def mutate(settings):
            settings.update(copied_updates)
            return True, None

        self.mutate_app_settings(mutate)


class CoordinatedMemoryConfigManager(MemoryConfigManager):
    def __init__(self, settings=None):
        super().__init__(settings)
        self.first_transaction_entered = threading.Event()
        self.release_first_transaction = threading.Event()
        self._transaction_count = 0

    def mutate_app_settings(self, mutator):
        with self._lock:
            self._transaction_count += 1
            if self._transaction_count == 1:
                self.first_transaction_entered.set()
                assert self.release_first_transaction.wait(timeout=5)
            snapshot = copy.deepcopy(self.settings)
            should_save, result = mutator(snapshot)
            if type(should_save) is not bool:
                raise TypeError('should_save must be a bool')
            if should_save:
                self.update_calls.append(copy.deepcopy(snapshot))
                self.settings = copy.deepcopy(snapshot)
            return result


class TornReadConfigManager(MemoryConfigManager):
    """Separate key reads fabricate a state that never existed coherently."""

    def get_app_setting(self, key, default=None):
        torn_values = {
            'error_reporting_state': ConsentState.ENABLED.value,
            'error_reporting_consent_version': CONSENT_VERSION,
            'error_reporting_prompt_count': 0,
        }
        return torn_values.get(key, default)


def make_policy(settings=None, manager_type=MemoryConfigManager):
    config_manager = manager_type(settings)
    return ConsentPolicy(config_manager), config_manager


def claimed_settings(
    state,
    count,
    deferred_until=None,
    installation_id=INSTALLATION_ID,
    claim_id=None,
    consent_generation=None,
):
    return {
        'error_reporting_state': state.value,
        'error_reporting_consent_version': CONSENT_VERSION,
        'error_reporting_prompt_count': count,
        'error_reporting_deferred_until': deferred_until,
        'error_reporting_installation_id': installation_id,
        'error_reporting_prompt_claim_id': claim_id,
        'error_reporting_consent_generation': consent_generation,
    }


def assert_uuid4(value):
    parsed = uuid.UUID(value)
    assert parsed.version == 4
    assert parsed.variant == uuid.RFC_4122


def install_claim_ids(monkeypatch):
    claim_ids = iter([
        uuid.UUID(CLAIM_A),
        uuid.UUID(CLAIM_B),
        uuid.UUID(GENERATION_A),
    ])
    monkeypatch.setattr(
        'src.core.error_report_consent.uuid.uuid4',
        lambda: next(claim_ids),
    )


def test_initial_state_preview_is_read_only_and_rejects_naive_now():
    policy, config_manager = make_policy()

    assert policy.should_prompt(NOW) is True
    assert policy.should_prompt(NOW.replace(tzinfo=None)) is False
    assert config_manager.update_calls == []


def test_first_claim_persists_the_display_before_an_outcome_exists():
    policy, config_manager = make_policy()

    claim_id = policy.claim_prompt(NOW)

    assert_uuid4(claim_id)
    assert config_manager.settings['error_reporting_state'] == ConsentState.DEFERRED.value
    assert config_manager.settings['error_reporting_consent_version'] == CONSENT_VERSION
    assert config_manager.settings['error_reporting_prompt_count'] == 1
    assert config_manager.settings['error_reporting_deferred_until'] == (
        '2026-08-13T12:00:00+00:00'
    )
    assert_uuid4(config_manager.settings['error_reporting_installation_id'])
    assert config_manager.settings['error_reporting_prompt_claim_id'] == claim_id
    assert CONSENT_KEYS <= config_manager.update_calls[-1].keys()


def test_first_claim_waits_for_thirty_full_days_before_second_claim():
    policy, config_manager = make_policy()

    first_claim_id = policy.claim_prompt(NOW)
    assert_uuid4(first_claim_id)
    assert policy.should_prompt(NOW + timedelta(days=29)) is False
    assert policy.claim_prompt(NOW + timedelta(days=29)) is None
    assert policy.should_prompt(NOW + timedelta(days=30)) is True
    second_claim_id = policy.claim_prompt(NOW + timedelta(days=30))
    assert_uuid4(second_claim_id)
    assert second_claim_id != first_claim_id

    assert config_manager.settings['error_reporting_state'] == (
        ConsentState.PROMPT_EXHAUSTED.value
    )
    assert config_manager.settings['error_reporting_prompt_count'] == 2
    assert config_manager.settings['error_reporting_deferred_until'] is None
    assert config_manager.settings['error_reporting_prompt_claim_id'] == second_claim_id


@pytest.mark.parametrize(
    'outcome,suppress',
    [
        (PromptOutcome.ENABLE, False),
        (PromptOutcome.LATER, True),
        (PromptOutcome.LATER, False),
    ],
)
def test_stale_first_claim_outcomes_cannot_replace_the_second_claim(
    monkeypatch,
    outcome,
    suppress,
):
    install_claim_ids(monkeypatch)
    policy, config_manager = make_policy({
        'error_reporting_installation_id': INSTALLATION_ID,
    })

    assert policy.claim_prompt(NOW) == CLAIM_A
    assert policy.claim_prompt(NOW + timedelta(days=30)) == CLAIM_B
    second_claim = dict(config_manager.settings)

    policy.record_outcome(
        CLAIM_A,
        outcome,
        NOW + timedelta(days=30),
        suppress=suppress,
    )

    assert config_manager.settings == second_claim


@pytest.mark.parametrize(
    'outcome,suppress,expected_state',
    [
        (PromptOutcome.ENABLE, False, ConsentState.ENABLED),
        (PromptOutcome.LATER, True, ConsentState.SUPPRESSED),
        (PromptOutcome.LATER, False, ConsentState.PROMPT_EXHAUSTED),
    ],
)
def test_current_second_claim_outcomes_apply_and_consume_the_claim(
    monkeypatch,
    outcome,
    suppress,
    expected_state,
):
    install_claim_ids(monkeypatch)
    policy, config_manager = make_policy({
        'error_reporting_installation_id': INSTALLATION_ID,
    })

    assert policy.claim_prompt(NOW) == CLAIM_A
    assert policy.claim_prompt(NOW + timedelta(days=30)) == CLAIM_B

    policy.record_outcome(
        CLAIM_B,
        outcome,
        NOW + timedelta(days=30),
        suppress=suppress,
    )

    assert config_manager.settings['error_reporting_state'] == expected_state.value
    assert config_manager.settings['error_reporting_prompt_count'] == 2
    assert config_manager.settings['error_reporting_deferred_until'] is None
    assert config_manager.settings['error_reporting_prompt_claim_id'] is None


@pytest.mark.parametrize('invalid_suppress', [0, 1, -1, 'false', 'true', None])
def test_record_outcome_rejects_non_bool_suppress_without_mutation(invalid_suppress):
    policy, config_manager = make_policy(
        claimed_settings(
            ConsentState.DEFERRED,
            1,
            (NOW + timedelta(days=30)).isoformat(),
            claim_id=CLAIM_A,
        )
    )
    original_settings = copy.deepcopy(config_manager.settings)

    with pytest.raises(TypeError, match='suppress must be a bool'):
        policy.record_outcome(
            CLAIM_A,
            PromptOutcome.LATER,
            NOW,
            suppress=invalid_suppress,
        )

    assert config_manager.settings == original_settings
    assert config_manager.update_calls == []


def test_lost_outcomes_can_never_produce_more_than_two_displays():
    policy, config_manager = make_policy()

    claims = [policy.claim_prompt(NOW)]
    claims.append(policy.claim_prompt(NOW + timedelta(days=30)))
    claims.append(policy.claim_prompt(NOW + timedelta(days=365)))

    assert [claim is not None for claim in claims] == [True, True, False]
    assert config_manager.settings['error_reporting_prompt_count'] == 2
    assert config_manager.settings['error_reporting_state'] == (
        ConsentState.PROMPT_EXHAUSTED.value
    )


def test_concurrent_initial_claims_allow_exactly_one_display():
    policy, config_manager = make_policy(manager_type=CoordinatedMemoryConfigManager)
    results = []

    first = threading.Thread(target=lambda: results.append(policy.claim_prompt(NOW)))
    second = threading.Thread(target=lambda: results.append(policy.claim_prompt(NOW)))
    first.start()
    assert config_manager.first_transaction_entered.wait(timeout=5)
    second.start()
    config_manager.release_first_transaction.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 1
    assert config_manager.settings['error_reporting_prompt_count'] == 1
    assert len(config_manager.update_calls) == 1


def test_concurrent_second_claims_allow_exactly_one_final_display():
    policy, config_manager = make_policy(
        claimed_settings(
            ConsentState.DEFERRED,
            1,
            (NOW - timedelta(seconds=1)).isoformat(),
        ),
        manager_type=CoordinatedMemoryConfigManager,
    )
    results = []

    first = threading.Thread(target=lambda: results.append(policy.claim_prompt(NOW)))
    second = threading.Thread(target=lambda: results.append(policy.claim_prompt(NOW)))
    first.start()
    assert config_manager.first_transaction_entered.wait(timeout=5)
    second.start()
    config_manager.release_first_transaction.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 1
    assert config_manager.settings['error_reporting_prompt_count'] == 2
    assert config_manager.settings['error_reporting_state'] == (
        ConsentState.PROMPT_EXHAUSTED.value
    )
    assert len(config_manager.update_calls) == 1


def test_record_outcome_requires_an_existing_current_version_claim():
    policy, config_manager = make_policy()

    policy.record_outcome(CLAIM_A, PromptOutcome.ENABLE, NOW)

    assert config_manager.settings == {}
    assert config_manager.update_calls == []


def test_later_outcome_clears_the_claim_and_preserves_the_preclaimed_state():
    policy, config_manager = make_policy()
    claim_id = policy.claim_prompt(NOW)
    claimed = dict(config_manager.settings)

    policy.record_outcome(claim_id, PromptOutcome.LATER, NOW)

    assert config_manager.settings == {
        **claimed,
        'error_reporting_prompt_claim_id': None,
        'error_reporting_consent_generation': None,
    }


def test_enable_outcome_overrides_first_claim_without_incrementing_exposure():
    policy, config_manager = make_policy()
    claim_id = policy.claim_prompt(NOW)

    policy.record_outcome(claim_id, PromptOutcome.ENABLE, NOW)

    assert config_manager.settings['error_reporting_state'] == ConsentState.ENABLED.value
    assert config_manager.settings['error_reporting_prompt_count'] == 1
    assert config_manager.settings['error_reporting_deferred_until'] is None
    assert config_manager.settings['error_reporting_prompt_claim_id'] is None
    assert policy.is_enabled() is True
    assert CONSENT_KEYS <= config_manager.update_calls[-1].keys()


def test_suppress_outcome_overrides_second_claim_without_incrementing_exposure():
    policy, config_manager = make_policy()
    policy.claim_prompt(NOW)
    claim_id = policy.claim_prompt(NOW + timedelta(days=30))

    policy.record_outcome(
        claim_id,
        PromptOutcome.LATER,
        NOW + timedelta(days=30),
        suppress=True,
    )

    assert config_manager.settings['error_reporting_state'] == ConsentState.SUPPRESSED.value
    assert config_manager.settings['error_reporting_prompt_count'] == 2
    assert config_manager.settings['error_reporting_deferred_until'] is None
    assert config_manager.settings['error_reporting_prompt_claim_id'] is None


@pytest.mark.parametrize(
    'terminal_state',
    [ConsentState.ENABLED, ConsentState.DISABLED_BY_USER, ConsentState.SUPPRESSED],
)
def test_stale_outcome_never_overwrites_a_terminal_state(terminal_state):
    policy, config_manager = make_policy()
    claim_id = policy.claim_prompt(NOW)
    policy.set_enabled(terminal_state == ConsentState.ENABLED)
    if terminal_state == ConsentState.SUPPRESSED:
        config_manager.settings.update(claimed_settings(ConsentState.SUPPRESSED, 1))
    update_count = len(config_manager.update_calls)

    policy.record_outcome(claim_id, PromptOutcome.ENABLE, NOW)

    assert config_manager.settings['error_reporting_state'] == terminal_state.value
    assert len(config_manager.update_calls) == update_count


def test_settings_disable_wins_before_a_concurrent_stale_enable_outcome():
    policy, config_manager = make_policy(
        claimed_settings(
            ConsentState.DEFERRED,
            1,
            (NOW + timedelta(days=30)).isoformat(),
            claim_id=CLAIM_A,
        ),
        manager_type=CoordinatedMemoryConfigManager,
    )

    disable = threading.Thread(target=lambda: policy.set_enabled(False))
    stale_outcome = threading.Thread(
        target=lambda: policy.record_outcome(CLAIM_A, PromptOutcome.ENABLE, NOW)
    )
    disable.start()
    assert config_manager.first_transaction_entered.wait(timeout=5)
    stale_outcome.start()
    config_manager.release_first_transaction.set()
    disable.join(timeout=5)
    stale_outcome.join(timeout=5)

    assert not disable.is_alive()
    assert not stale_outcome.is_alive()
    assert config_manager.settings['error_reporting_state'] == (
        ConsentState.DISABLED_BY_USER.value
    )
    assert len(config_manager.update_calls) == 1


def test_set_enabled_and_is_enabled_use_coherent_settings_snapshots():
    policy, config_manager = make_policy(
        {
            'unrelated': 'preserved',
            'error_reporting_prompt_claim_id': CLAIM_A,
        },
        manager_type=TornReadConfigManager,
    )

    assert policy.is_enabled() is False
    policy.set_enabled(True)

    assert config_manager.settings['unrelated'] == 'preserved'
    assert config_manager.settings['error_reporting_state'] == ConsentState.ENABLED.value
    assert config_manager.settings['error_reporting_prompt_count'] == 0
    assert config_manager.settings['error_reporting_prompt_claim_id'] is None
    assert_uuid4(config_manager.settings['error_reporting_consent_generation'])
    assert (
        config_manager.settings['error_reporting_consent_generation']
        != config_manager.settings['error_reporting_installation_id']
    )
    assert policy.is_enabled() is True


@pytest.mark.parametrize('enabled', [None, 0, 1, 'false', []])
def test_set_enabled_rejects_non_bool_values_without_writing(enabled):
    policy, config_manager = make_policy()

    with pytest.raises(TypeError, match='enabled must be a bool'):
        policy.set_enabled(enabled)

    assert config_manager.settings == {}
    assert config_manager.update_calls == []


def test_is_enabled_rejects_an_otherwise_coherent_state_with_an_active_claim():
    policy, _ = make_policy(
        claimed_settings(ConsentState.ENABLED, 1, claim_id=CLAIM_A)
    )

    assert policy.is_enabled() is False


def test_should_prompt_uses_one_coherent_snapshot():
    policy, _ = make_policy(
        claimed_settings(ConsentState.DISABLED_BY_USER, 1),
        manager_type=TornReadConfigManager,
    )

    assert policy.should_prompt(NOW) is False


@pytest.mark.parametrize(
    'old_state,old_count',
    [
        (ConsentState.ENABLED, 2),
        (ConsentState.PROMPT_EXHAUSTED, 2),
    ],
)
def test_old_version_enabled_or_exhausted_starts_a_fresh_claim_cycle(
    old_state,
    old_count,
):
    policy, config_manager = make_policy({
        'error_reporting_state': old_state.value,
        'error_reporting_consent_version': CONSENT_VERSION - 1,
        'error_reporting_prompt_count': old_count,
        'error_reporting_deferred_until': None,
        'error_reporting_installation_id': INSTALLATION_ID,
    })

    assert policy.should_prompt(NOW) is True
    claim_id = policy.claim_prompt(NOW)
    assert_uuid4(claim_id)

    assert config_manager.settings['error_reporting_state'] == ConsentState.DEFERRED.value
    assert config_manager.settings['error_reporting_consent_version'] == CONSENT_VERSION
    assert config_manager.settings['error_reporting_prompt_count'] == 1
    assert config_manager.settings['error_reporting_installation_id'] == INSTALLATION_ID


@pytest.mark.parametrize(
    'terminal_state',
    [ConsentState.SUPPRESSED, ConsentState.DISABLED_BY_USER],
)
def test_suppressed_and_user_disabled_are_terminal_across_versions(terminal_state):
    policy, config_manager = make_policy({
        'error_reporting_state': terminal_state.value,
        'error_reporting_consent_version': CONSENT_VERSION - 1,
        'error_reporting_prompt_count': 1,
    })

    assert policy.should_prompt(NOW) is False
    assert policy.claim_prompt(NOW) is None
    assert config_manager.update_calls == []


@pytest.mark.parametrize(
    'settings',
    [
        {
            'error_reporting_state': ConsentState.DEFERRED.value,
            'error_reporting_consent_version': CONSENT_VERSION,
            'error_reporting_prompt_count': 1,
            'error_reporting_deferred_until': 'not-a-timestamp',
        },
        {
            'error_reporting_state': ConsentState.DEFERRED.value,
            'error_reporting_consent_version': CONSENT_VERSION,
            'error_reporting_prompt_count': 1,
            'error_reporting_deferred_until': '2026-08-13T12:00:00',
        },
        {
            'error_reporting_state': ConsentState.DEFERRED.value,
            'error_reporting_consent_version': CONSENT_VERSION,
            'error_reporting_prompt_count': 7,
            'error_reporting_deferred_until': '2026-08-13T12:00:00+00:00',
        },
        {
            'error_reporting_state': ConsentState.ENABLED.value,
            'error_reporting_consent_version': CONSENT_VERSION + 1,
            'error_reporting_prompt_count': 1,
        },
        {
            'error_reporting_consent_version': False,
            'error_reporting_prompt_count': 0,
        },
        {
            'error_reporting_consent_version': CONSENT_VERSION,
            'error_reporting_prompt_count': False,
        },
        {
            'error_reporting_state': ConsentState.ENABLED.value,
            'error_reporting_consent_version': CONSENT_VERSION - 1,
            'error_reporting_prompt_count': 1,
            'error_reporting_deferred_until': 'not-a-timestamp',
        },
    ],
)
def test_corrupt_naive_or_future_version_values_fail_closed(settings):
    policy, config_manager = make_policy(settings)

    assert policy.should_prompt(NOW + timedelta(days=365)) is False
    assert policy.claim_prompt(NOW + timedelta(days=365)) is None
    assert config_manager.update_calls == []


def test_corrupt_current_enabled_state_is_not_treated_as_enabled():
    policy, _ = make_policy({
        'error_reporting_state': ConsentState.ENABLED.value,
        'error_reporting_consent_version': CONSENT_VERSION,
        'error_reporting_prompt_count': 'one',
        'error_reporting_deferred_until': None,
    })

    assert policy.is_enabled() is False


def test_future_defer_time_is_ineligible_until_the_clock_reaches_it():
    deferred_until = NOW + timedelta(days=31)
    policy, _ = make_policy(
        claimed_settings(ConsentState.DEFERRED, 1, deferred_until.isoformat())
    )

    assert policy.should_prompt(NOW) is False
    assert policy.should_prompt(deferred_until) is True


@pytest.mark.parametrize(
    'existing_id',
    [
        None,
        'invalid',
        str(uuid.uuid1()),
        INSTALLATION_ID.upper(),
        '00000000-0000-4000-8000-000000000000',
    ],
)
def test_claim_generates_a_uuid4_when_the_installation_id_is_missing_or_invalid(
    existing_id,
):
    settings = {}
    if existing_id is not None:
        settings['error_reporting_installation_id'] = existing_id
    policy, config_manager = make_policy(settings)

    claim_id = policy.claim_prompt(NOW)
    assert_uuid4(claim_id)

    assert_uuid4(config_manager.settings['error_reporting_installation_id'])
    assert config_manager.settings['error_reporting_installation_id'] != existing_id


def test_every_consent_mutation_preserves_an_existing_valid_uuid4():
    policy, config_manager = make_policy({
        'error_reporting_installation_id': INSTALLATION_ID,
    })

    claim_id = policy.claim_prompt(NOW)
    assert config_manager.settings['error_reporting_installation_id'] == INSTALLATION_ID
    policy.record_outcome(claim_id, PromptOutcome.ENABLE, NOW)
    assert config_manager.settings['error_reporting_installation_id'] == INSTALLATION_ID
    policy.set_enabled(False)
    assert config_manager.settings['error_reporting_installation_id'] == INSTALLATION_ID
    assert all(CONSENT_KEYS <= update.keys() for update in config_manager.update_calls)


def test_release_first_prompt_claim_restores_immediate_eligibility():
    policy, config_manager = make_policy({
        'error_reporting_installation_id': INSTALLATION_ID,
    })
    claim_id = policy.claim_prompt(NOW)

    policy.release_prompt_claim(claim_id, NOW)

    assert config_manager.settings['error_reporting_state'] == ConsentState.UNDECIDED.value
    assert config_manager.settings['error_reporting_consent_version'] == CONSENT_VERSION
    assert config_manager.settings['error_reporting_prompt_count'] == 0
    assert config_manager.settings['error_reporting_deferred_until'] is None
    assert config_manager.settings['error_reporting_prompt_claim_id'] is None
    assert config_manager.settings['error_reporting_installation_id'] == INSTALLATION_ID
    assert policy.should_prompt(NOW) is True


def test_release_second_prompt_claim_restores_due_deferred_state():
    policy, config_manager = make_policy(
        claimed_settings(
            ConsentState.DEFERRED,
            1,
            NOW.isoformat(),
            installation_id=INSTALLATION_ID,
        )
    )
    claim_id = policy.claim_prompt(NOW)

    policy.release_prompt_claim(claim_id, NOW)

    assert config_manager.settings['error_reporting_state'] == ConsentState.DEFERRED.value
    assert config_manager.settings['error_reporting_prompt_count'] == 1
    assert config_manager.settings['error_reporting_deferred_until'] == NOW.isoformat()
    assert config_manager.settings['error_reporting_prompt_claim_id'] is None
    assert config_manager.settings['error_reporting_installation_id'] == INSTALLATION_ID
    assert policy.should_prompt(NOW) is True


def test_release_prompt_claim_ignores_stale_token_without_writing():
    policy, config_manager = make_policy(
        claimed_settings(
            ConsentState.DEFERRED,
            1,
            (NOW + timedelta(days=30)).isoformat(),
            installation_id=INSTALLATION_ID,
            claim_id=CLAIM_B,
        )
    )

    policy.release_prompt_claim(CLAIM_A, NOW)

    assert config_manager.update_calls == []
    assert config_manager.settings['error_reporting_prompt_claim_id'] == CLAIM_B


def test_release_prompt_claim_rejects_naive_time_without_writing():
    policy, config_manager = make_policy(
        claimed_settings(
            ConsentState.DEFERRED,
            1,
            (NOW + timedelta(days=30)).isoformat(),
            claim_id=CLAIM_A,
        )
    )

    with pytest.raises(ValueError, match='now must be a timezone-aware datetime'):
        policy.release_prompt_claim(CLAIM_A, NOW.replace(tzinfo=None))

    assert config_manager.update_calls == []


def test_outcome_and_settings_mutations_repair_an_invalid_installation_id():
    policy, config_manager = make_policy(
        claimed_settings(
            ConsentState.DEFERRED,
            1,
            (NOW + timedelta(days=30)).isoformat(),
            installation_id='invalid',
            claim_id=CLAIM_A,
        )
    )

    policy.record_outcome(CLAIM_A, PromptOutcome.ENABLE, NOW)
    assert_uuid4(config_manager.settings['error_reporting_installation_id'])

    config_manager.settings['error_reporting_installation_id'] = 'still-invalid'
    policy.set_enabled(False)
    assert_uuid4(config_manager.settings['error_reporting_installation_id'])


def test_set_enabled_repairs_all_consent_fields_and_clears_any_claim():
    policy, config_manager = make_policy({
        'error_reporting_state': 'corrupt',
        'error_reporting_consent_version': 'old',
        'error_reporting_prompt_count': 99,
        'error_reporting_deferred_until': 'not-a-time',
        'error_reporting_installation_id': 'invalid',
        'error_reporting_prompt_claim_id': CLAIM_A,
    })

    policy.set_enabled(True)

    assert config_manager.settings['error_reporting_state'] == ConsentState.ENABLED.value
    assert config_manager.settings['error_reporting_consent_version'] == CONSENT_VERSION
    assert config_manager.settings['error_reporting_prompt_count'] == 0
    assert config_manager.settings['error_reporting_deferred_until'] is None
    assert_uuid4(config_manager.settings['error_reporting_installation_id'])
    assert config_manager.settings['error_reporting_prompt_claim_id'] is None
    assert_uuid4(config_manager.settings['error_reporting_consent_generation'])
    assert (
        config_manager.settings['error_reporting_consent_generation']
        != config_manager.settings['error_reporting_installation_id']
    )


def test_prompt_claim_id_is_local_only_and_never_enters_a_report_payload(monkeypatch):
    policy, config_manager = make_policy({
        'error_reporting_installation_id': INSTALLATION_ID,
    })
    claim_id = policy.claim_prompt(NOW)
    monkeypatch.setattr(
        'src.core.error_report_builder.collect_environment',
        lambda: {
            'app': {
                'version': '2.3.1',
                'package_kind': 'source',
                'ui_language': 'en',
            },
            'system': {
                'os_family': 'linux',
                'os_version': '6.8.0',
                'architecture': 'x86_64',
                'locale': 'en_US',
                'utc_offset_minutes': 0,
            },
            'runtime': {
                'python_version': '3.12.4',
                'qt_version': '6.7.2',
                'rust_core_version': '2.3.1',
            },
        },
    )

    payload = build_error_report(
        config_manager,
        operation_kind='export',
        db_engine='mysql',
        phase='dump.run',
        error_message='synthetic failure',
    )

    serialized = json.dumps(payload, sort_keys=True)
    assert 'error_reporting_prompt_claim_id' not in serialized
    assert claim_id not in serialized


def test_legacy_github_auto_report_never_counts_as_affirmative_consent():
    policy, _ = make_policy({'github_auto_report': True})

    assert policy.is_enabled() is False
    assert policy.should_prompt(NOW) is True


@pytest.mark.parametrize('stored_generation', [None, 'corrupt-token'])
def test_capture_submission_token_atomically_migrates_only_current_enabled_state(
    stored_generation,
):
    settings = claimed_settings(
        ConsentState.ENABLED,
        1,
        consent_generation=stored_generation,
    )
    policy, config_manager = make_policy(settings)

    token = policy.capture_submission_token()

    assert_uuid4(token)
    assert token == config_manager.settings['error_reporting_consent_generation']
    assert token != config_manager.settings['error_reporting_installation_id']
    assert len(config_manager.update_calls) == 1
    assert policy.is_submission_token_current(token) is True


def test_capture_submission_token_reuses_valid_generation_without_writing():
    policy, config_manager = make_policy(
        claimed_settings(
            ConsentState.ENABLED,
            1,
            consent_generation=GENERATION_A,
        )
    )

    assert policy.capture_submission_token() == GENERATION_A
    assert config_manager.update_calls == []


@pytest.mark.parametrize(
    'settings',
    [
        claimed_settings(ConsentState.DISABLED_BY_USER, 1),
        claimed_settings(ConsentState.DEFERRED, 1, NOW.isoformat()),
        claimed_settings(ConsentState.ENABLED, 1, claim_id=CLAIM_A),
        {'github_auto_report': True},
    ],
)
def test_capture_submission_token_fails_closed_without_affirmative_consent(settings):
    policy, config_manager = make_policy(settings)
    original = copy.deepcopy(config_manager.settings)

    assert policy.capture_submission_token() is None
    assert config_manager.settings == original
    assert config_manager.update_calls == []


@pytest.mark.parametrize(
    'stored_generation,supplied_generation',
    [
        (None, GENERATION_A),
        ('corrupt-token', GENERATION_A),
        (GENERATION_A, None),
        (GENERATION_A, 'corrupt-token'),
        (GENERATION_A, GENERATION_B),
    ],
)
def test_submission_token_validation_fails_closed_on_missing_corrupt_or_stale_token(
    stored_generation, supplied_generation
):
    policy, _ = make_policy(
        claimed_settings(
            ConsentState.ENABLED,
            1,
            consent_generation=stored_generation,
        )
    )

    assert policy.is_submission_token_current(supplied_generation) is False


def test_disable_and_reenable_each_invalidate_pending_submission_token():
    policy, config_manager = make_policy()
    policy.set_enabled(True)
    first_token = config_manager.settings['error_reporting_consent_generation']

    policy.set_enabled(False)
    assert config_manager.settings['error_reporting_consent_generation'] is None
    assert policy.is_submission_token_current(first_token) is False

    policy.set_enabled(True)
    second_token = config_manager.settings['error_reporting_consent_generation']

    assert_uuid4(first_token)
    assert_uuid4(second_token)
    assert second_token != first_token
    assert policy.is_submission_token_current(first_token) is False
    assert policy.is_submission_token_current(second_token) is True


def test_revoked_pending_token_cannot_receive_dispatch_authorization_after_reenable():
    policy, _ = make_policy()
    policy.set_enabled(True)
    stale_token = policy.capture_submission_token()

    policy.set_enabled(False)
    policy.set_enabled(True)

    assert policy.authorize_submission(stale_token) is False


def test_settings_disable_returns_while_committed_session_post_is_blocked():
    from src.core.error_report_transport import ErrorReportTransport

    policy, config_manager = make_policy()
    policy.set_enabled(True)
    token = policy.capture_submission_token()
    post_entered = threading.Event()
    release_post = threading.Event()

    class BlockingSession:
        trust_env = True

        def send(self, _request, **_kwargs):
            post_entered.set()
            assert release_post.wait(timeout=5)
            return type("Response", (), {
                "status_code": 202,
                "headers": {},
                "iter_content": lambda self, chunk_size: iter([
                    b'{"status":"accepted","receipt":'
                    b'"11111111-1111-4111-8111-111111111111"}'
                ]),
                "close": lambda self: None,
            })()

    transport = ErrorReportTransport(
        "https://relay.example.test/v1/reports",
        session=BlockingSession(),
        submission_authorizer=lambda: policy.authorize_submission(token),
    )
    request_thread = threading.Thread(
        target=lambda: transport.submit({"safe": True})
    )
    request_thread.start()
    assert post_entered.wait(timeout=5)

    mutation_thread = threading.Thread(target=lambda: policy.set_enabled(False))
    mutation_thread.start()
    mutation_thread.join(timeout=1)

    assert not mutation_thread.is_alive()
    assert policy.is_enabled() is False
    assert request_thread.is_alive()

    release_post.set()
    request_thread.join(timeout=5)
    assert not request_thread.is_alive()


@pytest.mark.parametrize("reenable", [False, True])
def test_revocation_before_dispatch_authorization_prevents_post(reenable):
    from src.core.error_report_transport import ErrorReportTransport

    policy, _ = make_policy()
    policy.set_enabled(True)
    token = policy.capture_submission_token()
    policy.set_enabled(False)
    if reenable:
        policy.set_enabled(True)

    class NoPostSession:
        trust_env = True

        def send(self, _request, **_kwargs):
            pytest.fail("revocation before authorization must prevent dispatch")

    result = ErrorReportTransport(
        "https://relay.example.test/v1/reports",
        session=NoPostSession(),
        submission_authorizer=lambda: policy.authorize_submission(token),
    ).submit({"safe": True})

    assert result.success is False
    assert result.message == "Relay request cancelled."


@pytest.mark.parametrize("reenable", [False, True])
def test_concurrent_revocation_before_dispatch_permit_prevents_post(reenable):
    from src.core.error_report_transport import ErrorReportTransport

    policy, _ = make_policy()
    policy.set_enabled(True)
    token = policy.capture_submission_token()
    authorization_waiting = threading.Event()
    release_authorization = threading.Event()
    results = []

    def authorize_after_release():
        authorization_waiting.set()
        assert release_authorization.wait(timeout=5)
        return policy.authorize_submission(token)

    class NoPostSession:
        trust_env = True

        def send(self, _request, **_kwargs):
            pytest.fail("pre-permit revocation must prevent dispatch")

    transport = ErrorReportTransport(
        "https://relay.example.test/v1/reports",
        session=NoPostSession(),
        submission_authorizer=authorize_after_release,
    )
    request_thread = threading.Thread(
        target=lambda: results.append(transport.submit({"safe": True}))
    )
    request_thread.start()
    assert authorization_waiting.wait(timeout=5)

    policy.set_enabled(False)
    if reenable:
        policy.set_enabled(True)
    release_authorization.set()
    request_thread.join(timeout=5)

    assert not request_thread.is_alive()
    assert results[0].message == "Relay request cancelled."


def test_post_permit_disable_is_nonblocking_before_python_enters_session_post():
    from src.core.error_report_transport import ErrorReportTransport

    policy, _ = make_policy()
    policy.set_enabled(True)
    token = policy.capture_submission_token()
    permit_committed = threading.Event()
    release_authorizer = threading.Event()
    post_entered = threading.Event()
    results = []

    def authorize_then_pause():
        allowed = policy.authorize_submission(token)
        assert allowed is True
        permit_committed.set()
        assert release_authorizer.wait(timeout=5)
        return allowed

    class RecordingSession:
        trust_env = True

        def send(self, _request, **_kwargs):
            post_entered.set()
            return type("Response", (), {
                "status_code": 202,
                "headers": {},
                "iter_content": lambda self, chunk_size: iter([
                    b'{"status":"accepted","receipt":'
                    b'"11111111-1111-4111-8111-111111111111"}'
                ]),
                "close": lambda self: None,
            })()

    transport = ErrorReportTransport(
        "https://relay.example.test/v1/reports",
        session=RecordingSession(),
        submission_authorizer=authorize_then_pause,
    )
    request_thread = threading.Thread(
        target=lambda: results.append(transport.submit({"safe": True}))
    )
    request_thread.start()
    assert permit_committed.wait(timeout=5)
    assert not post_entered.is_set()

    mutation_thread = threading.Thread(target=lambda: policy.set_enabled(False))
    mutation_thread.start()
    mutation_thread.join(timeout=1)

    assert not mutation_thread.is_alive()
    assert policy.is_enabled() is False
    assert not post_entered.is_set()

    release_authorizer.set()
    request_thread.join(timeout=5)

    assert not request_thread.is_alive()
    assert post_entered.is_set()
    assert results[0].success is True


def test_consent_generation_is_local_only_and_never_enters_report_payload(monkeypatch):
    policy, config_manager = make_policy()
    policy.set_enabled(True)
    generation = config_manager.settings['error_reporting_consent_generation']
    monkeypatch.setattr(
        'src.core.error_report_builder.collect_environment',
        lambda: {
            'app': {
                'version': '2.3.1',
                'package_kind': 'source',
                'ui_language': 'en',
            },
            'system': {
                'os_family': 'linux',
                'os_version': '6.8.0',
                'architecture': 'x86_64',
                'locale': 'en_US',
                'utc_offset_minutes': 0,
            },
            'runtime': {
                'python_version': '3.12.4',
                'qt_version': '6.7.2',
                'rust_core_version': '2.3.1',
            },
        },
    )

    payload = build_error_report(
        config_manager,
        operation_kind='export',
        db_engine='mysql',
        phase='dump.run',
        error_message='synthetic failure',
    )
    serialized = json.dumps(payload, sort_keys=True)

    assert 'error_reporting_consent_generation' not in serialized
    assert generation not in serialized
