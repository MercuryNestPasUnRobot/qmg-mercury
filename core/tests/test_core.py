from copy import deepcopy
import json
import random

import pytest

from qmg_core import (CoreAPI, CoreError, GameState, LegalActionGenerator, MapGraph, OneStepPlanner,
                      OperationLogger, SaveRepository, ScoreEvaluator, ScoreResolver, SupplyResolver,
                      ZoneRegistry, observation, validate_state)
from qmg_core.model import TURN_ORDER, digest, zone
from conftest import make, move_initial, card, play, choose, finish_choices


def test_map_and_seed_are_independent_of_gui(data):
    p, q = make(data, seed=1), make(data, seed=1)
    assert p.state.to_dict() == q.state.to_dict()
    assert p.state.positions['germany:DECK'] == ['g-deck-2', 'g-deck-1']
    assert make(data, seed=1000).state.positions['germany:DECK'] != p.state.positions['germany:DECK']
    graph = MapGraph(p.rules)
    assert graph.neighbors('A') == ['B', 'S']
    assert graph.distances('A')['D'] == 3


@pytest.mark.parametrize('mutation', ['duplicate', 'edge', 'instruction', 'ability', 'terrain', 'orphan'])
def test_invalid_content_is_rejected(data, mutation):
    if mutation == 'duplicate':
        data['regions'].append(deepcopy(data['regions'][0]))
    elif mutation == 'edge':
        data['connections'][0]['b'] = 'missing'
    elif mutation == 'instruction':
        card(data, 'g-build')['effects'] = [{'type': 'EXECUTE_PYTHON', 'code': 'anything'}]
    elif mutation == 'ability':
        card(data, 'g-build')['abilities'] = ['missing']
    elif mutation == 'terrain':
        move_initial(data, 'germany-army-0', 'S')
    else:
        data['initial_tokens'].append({'token_id': 'bad', 'definition_id': 'g-extra', 'token_type': 'ABILITY',
                                       'owner_country': 'germany', 'position_id': 'germany:ABILITY_READY', 'source_token_id': 'missing'})
    with pytest.raises(CoreError):
        make(data)


def test_state_copy_and_entity_invariants(data):
    p = make(data)
    state = p.state
    state.scores['axis'] = 500
    assert p.state.scores['axis'] == 0
    state.positions['A'].append('germany-army-0')
    with pytest.raises(CoreError):
        validate_state(p.rules, state)
    state = p.state
    ZoneRegistry.transfer(state, 'united-kingdom-army-0', 'A')
    with pytest.raises(CoreError):
        validate_state(p.rules, state)
    assert set(p.state.tokens['g-build'].__dict__) == {'token_id', 'definition_id', 'token_type', 'owner_country', 'position_id', 'source_token_id'}
    assert p.state.tokens['g-build'].definition_id == p.state.tokens['g-copy'].definition_id
    assert p.state.tokens['g-build'].token_id != p.state.tokens['g-copy'].token_id


def test_sequential_card_blocks_and_revalidates_commands(data):
    p = make(data)
    initial_count = len(p.state.tokens)
    play(p, 'g-chain')
    assert p.state.scores['axis'] == 1
    assert p.state.tokens['g-chain'].position_id == 'RESOLUTION'
    request = p.state.decision
    before = p.state.to_dict()
    with pytest.raises(CoreError, match='Waiting'):
        p.step()
    bad = {'type': 'CHOOSE', 'actor': 'germany', 'expected_version': p.state.version, 'decision_id': request.decision_id, 'option': 'D'}
    with pytest.raises(CoreError):
        p.submit(bad, ('germany',))
    assert p.state.to_dict() == before
    valid = p.legal_commands(('germany',))[0]
    with pytest.raises(CoreError):
        p.submit(valid, ('united-kingdom',))
    choose(p, 'B')
    assert p.state.scores['axis'] == 3
    assert p.state.tokens['g-chain'].position_id == 'germany:DISCARD'
    assert p.state.tokens['germany-army-1'].position_id == 'B'
    assert len(p.state.tokens) == initial_count
    with pytest.raises(CoreError):
        p.submit(valid, ('germany',))


def test_gui_and_ai_commands_have_identical_authority_results(data):
    p, q = make(data), make(data)
    gui, ai = CoreAPI(p, ('germany',)), CoreAPI(q, ('germany',))
    command = next(c for c in gui.legal_commands() if c.get('card') == 'g-chain')
    assert gui.submit(command) == ai.submit(command)
    choice = gui.legal_commands()[0]
    assert gui.submit(choice) == ai.submit(choice)
    assert p.state.to_dict() == q.state.to_dict()
    assert p.records == q.records


def test_build_and_recruit_use_distinct_eligibility(data):
    move_initial(data, 'germany-army-0', 'germany:RESERVE_ARMY')
    p = make(data)
    legal = LegalActionGenerator(p.rules)
    assert 'B' not in legal.targets(p.state, 'germany', {'type': 'BUILD_ARMY'})
    assert 'B' in legal.targets(p.state, 'germany', {'type': 'RECRUIT'})


def test_empty_battle_is_effective_and_continues_card(data):
    card(data, 'g-battle')['effects'] += [{'type': 'GAIN_VP', 'amount': 1}]
    p = make(data)
    play(p, 'g-battle')
    choose(p, 'B')
    battle = next(r for r in p.records if r['operation_type'] == 'LAND_BATTLE')
    assert battle['outcome']['success'] and battle['outcome']['changes'] == []
    assert p.state.scores['axis'] == 1
    assert [r['sequence_id'] for r in p.records] == list(range(1, p.state.version + 1))


def test_announcement_prevention_resolves_before_elimination(data):
    card(data, 'g-battle')['effects'] = [{'type': 'ELIMINATE', 'target_token': 'united-kingdom-army-0'}, {'type': 'GAIN_VP', 'amount': 50}]
    p = make(data)
    play(p, 'g-battle')
    assert p.state.tokens['united-kingdom-army-0'].position_id == 'E'
    assert p.state.scores['axis'] == 0  # Explicit fixture stop-on-failure policy.
    events = [r['operation_type'] for r in p.records]
    assert events.index('ANNOUNCE') < events.index('CANCEL') < events.index('ELIMINATE')
    assert not next(r for r in p.records if r['operation_type'] == 'ELIMINATE')['outcome']['success']
    assert not p.state.announcements


def test_direct_elimination_does_not_trigger_battle_ability(data):
    move_initial(data, 'g-status', 'germany:STATUS')
    move_initial(data, 'uk-status', 'united-kingdom:DISCARD')
    card(data, 'g-battle')['effects'] = [{'type': 'ELIMINATE', 'target_token': 'united-kingdom-army-0'}]
    p = make(data)
    play(p, 'g-battle')
    assert not p.state.decision
    assert p.state.tokens['united-kingdom-army-0'].position_id == 'united-kingdom:RESERVE_ARMY'
    assert p.state.scores['axis'] == 0
    assert not any(r['operation_type'] == 'LAND_BATTLE' for r in p.records)


def test_optional_paid_copy_returns_from_pool_via_turn_end_response(data):
    move_initial(data, 'g-status', 'germany:STATUS')
    card(data, 'g-battle')['effects'] *= 2
    p = make(data)
    play(p, 'g-battle')
    choose(p, 'B')
    assert p.state.decision.decision_type == 'ABILITY'
    choose(p, 'use')
    assert p.state.decision.decision_type == 'COST'
    choose(p, 'g-copy')
    assert p.state.scores['axis'] == 2
    assert p.state.decision.decision_type == 'TARGET'  # Second battle, not another activation.
    choose(p, 'B')
    ability = 'ability:g-status:g-extra'
    assert p.state.tokens[ability].position_id == 'germany:OBJECT_POOL'
    listener, = p.state.positions['germany:RESPONSE']
    assert p.state.tokens[listener].source_token_id == 'g-status'
    assert p.state.tokens['g-copy'].position_id == 'germany:DISCARD'
    assert p.state.scores['axis'] == 2
    for _ in range(5):
        p.submit(p.legal_commands((TURN_ORDER[p.state.turn.country_index],))[0], TURN_ORDER)
        finish_choices(p)
    assert p.state.turn.country_index == 1 and p.state.turn.phase == 'Play'
    replacement, = p.state.positions['germany:ABILITY_READY']
    assert p.state.tokens[replacement].definition_id == 'g-extra'
    assert replacement == ability and p.state.tokens[listener].position_id == 'germany:OBJECT_POOL'


def test_cost_selection_does_not_consume_partial_payment(data):
    move_initial(data, 'g-status', 'germany:STATUS')
    data['abilities'][0]['discard_cost'] = 2
    p = make(data)
    play(p, 'g-battle'); choose(p, 'B'); choose(p, 'use'); choose(p, 'g-copy')
    assert p.state.tokens['g-copy'].position_id == 'germany:HAND'
    assert 'g-copy' not in p.state.decision.legal_options
    choose(p, 'g-draw')
    payments = [r for r in p.records if r['operation_type'] == 'PAY_COST']
    assert len(payments) == 1
    assert {c['token_id'] for c in payments[0]['outcome']['changes']} >= {'g-copy', 'g-draw'}


def test_decline_and_checkpoint_reset_do_not_rescan_same_operation(data):
    move_initial(data, 'g-status', 'germany:STATUS')
    p = make(data)
    play(p, 'g-battle'); choose(p, 'B'); choose(p, 'decline')
    assert not p.state.decision and not p.state.stack
    assert p.state.scores['axis'] == 0
    data['abilities'][0].update(activation_mode='AUTO', reset_policy='CHECKPOINT', discard_cost=0)
    card(data, 'g-battle')['effects'] *= 2
    p = make(data)
    play(p, 'g-battle'); choose(p, 'B'); choose(p, 'B')
    assert p.state.scores['axis'] == 4
    assert p.state.tokens['ability:g-status:g-extra'].position_id == 'germany:ABILITY_READY'
    assert sum(r['operation_type'] == 'ABILITY_ACTIVATED' for r in p.records) == 2


def test_source_removal_invalidates_remaining_ability_effects(data):
    move_initial(data, 'g-status', 'germany:STATUS')
    data['abilities'][0].update(activation_mode='AUTO', discard_cost=0, effects=[
        {'type': 'TRANSFER', 'token': 'g-status', 'destination': 'germany:DISCARD'}, {'type': 'GAIN_VP', 'amount': 99}])
    p = make(data)
    play(p, 'g-battle'); choose(p, 'B')
    assert p.state.scores['axis'] == 0
    assert p.state.tokens['ability:g-status:g-extra'].position_id == 'germany:OBJECT_POOL'
    assert any(r['operation_type'] == 'ABILITY_INVALIDATED' for r in p.records)


def test_redeploy_recomputes_targets_and_preserves_unit_identity(data):
    move_initial(data, 'germany-army-1', 'B')
    p = make(data)
    play(p, 'g-transport')
    original = set(p.state.stack[0].eligible_token_ids)
    choose(p, 'germany-army-0')
    assert 'C' not in p.state.decision.legal_options  # B loses supply when A is temporarily removed.
    choose(p, 'A')
    choose(p, 'germany-army-1')
    assert 'C' not in p.state.decision.legal_options  # Cannot build using its own vacated B as support.
    choose(p, 'B')
    assert not p.state.stack and not p.state.decision
    assert p.state.tokens['germany-army-0'].position_id == 'A'
    assert p.state.tokens['germany-army-1'].position_id == 'B'
    assert original == {'germany-army-0', 'germany-army-1'}
    assert sum(r['outcome']['context'].get('redeployed', False) for r in p.records) == 2


def test_no_redeploy_destination_never_loses_original_unit(data):
    move_initial(data, 'germany-army-0', 'D')
    for region in data['regions']:
        if region['id'] == 'A':
            region.pop('home_country')
    p = make(data)
    play(p, 'g-transport'); choose(p, 'germany-army-0')
    assert not p.state.decision
    assert p.state.tokens['germany-army-0'].position_id == 'D'


def test_status_modifier_is_derived_from_position(data):
    card(data, 'g-status')['modifiers'] = [{'kind': 'build_region', 'unit_kind': 'ARMY', 'region': 'C'}]
    p = make(data)
    assert 'C' not in p.legal.targets(p.state, 'germany', {'type': 'BUILD_ARMY'})
    move_initial(data, 'g-status', 'germany:STATUS')
    p = make(data)
    assert 'C' in p.legal.targets(p.state, 'germany', {'type': 'BUILD_ARMY'})


def test_dynamic_score_source_and_static_site_are_counted_once(data):
    move_initial(data, 'germany-army-1', 'D')
    move_initial(data, 'dynamic-source', 'D')
    site = next(s for s in data['score_sites'] if s['id'] == 'dynamic-site')
    site['requires_supply'] = False
    data['initial_tokens'].append({**next(t for t in data['initial_tokens'] if t['token_id'] == 'dynamic-source'), 'token_id': 'dynamic-copy'})
    p = make(data)
    assert ScoreResolver(p.rules).sites(p.state, 'germany') == {'g-home': 1, 'dynamic-site': 3}


def test_supply_phase_uses_semantic_removal_and_correct_actor(data):
    move_initial(data, 'germany-army-1', 'D')
    p = make(data)
    assert 'germany-army-1' not in SupplyResolver(p.rules).supplied(p.state, 'germany')
    p.submit(p.legal_commands(('germany',))[0], ('germany',))
    assert p.state.turn.phase == 'Supply'
    assert p.state.tokens['germany-army-1'].position_id == 'germany:RESERVE_ARMY'
    assert next(r for r in p.records if r['operation_type'] == 'ELIMINATE')['actor'] == 'germany'


def test_six_country_phases_score_and_round_terminal(data):
    p = make(data)
    starts = []
    for _ in range(80):
        if p.state.winner:
            break
        if p.state.turn.phase == 'Play':
            starts.append(TURN_ORDER[p.state.turn.country_index])
        commands = p.legal_commands(TURN_ORDER)
        p.submit(next(c for c in commands if c['type'] == 'END_PHASE'), TURN_ORDER)
        finish_choices(p)
    assert starts == list(TURN_ORDER) * 2
    assert p.state.winner == 'axis' and p.state.turn.round_id == 3
    assert p.state.scores['axis'] == 2


def test_initial_discard_is_serial_and_seeded(data):
    data['settings'].update(initial_hand=1, setup_discard=1)
    # Every country needs an explicit fixture deck; no official opening counts assumed.
    for c in TURN_ORDER[2:]:
        definition = {'id': c + '-card', 'country': c, 'type': 'EVENT', 'effects': [], 'abilities': [], 'failure_policy': 'stop'}
        data['cards'].append(definition)
        data['initial_tokens'].append({'token_id': c + '-deck', 'definition_id': definition['id'], 'token_type': 'CARD', 'owner_country': c, 'position_id': zone(c, 'DECK')})
    p = make(data)
    requested = []
    while p.state.decision:
        requested.append(p.state.decision.required_country)
        choose(p, p.state.decision.legal_options[0])
    assert requested == list(TURN_ORDER)
    assert p.state.turn.phase == 'Play' and not p.state.stack


def test_journal_failure_publishes_no_state_or_record(data):
    class BrokenLogger:
        def append(self, record):
            raise OSError('disk full')
    p = make(data, logger=BrokenLogger())
    before = p.state.to_dict()
    with pytest.raises(OSError):
        play(p, 'g-build')
    assert p.state.to_dict() == before and p.records == []


def test_checkpoint_and_journal_resume_mid_decision(tmp_path, data):
    journal, snapshot = tmp_path / 'operations.jsonl', tmp_path / 'initial.json'
    p = make(data, logger=OperationLogger(journal))
    SaveRepository.save(snapshot, p)
    play(p, 'g-chain')
    resumed = SaveRepository.recover(snapshot, p.rules, journal)
    assert resumed.state.to_dict() == p.state.to_dict()
    SaveRepository.save(tmp_path / 'mid.json', p)
    mid = SaveRepository.recover(tmp_path / 'mid.json', p.rules, journal)
    assert mid.state.to_dict() == p.state.to_dict()
    choose(p, 'B'); choose(resumed, 'B'); choose(mid, 'B')
    assert resumed.state.to_dict() == p.state.to_dict() == mid.state.to_dict()
    assert resumed.record_head == p.record_head == mid.record_head
    restored = SaveRepository.recover(snapshot, p.rules, journal)
    assert restored.state.to_dict() == p.state.to_dict()


@pytest.mark.parametrize('corruption', ['tamper', 'reverse', 'duplicate', 'missing', 'tail', 'snapshot'])
def test_corrupt_persistence_is_rejected(tmp_path, data, corruption):
    journal, snapshot = tmp_path / 'ops.jsonl', tmp_path / 'state.json'
    p = make(data, logger=OperationLogger(journal))
    SaveRepository.save(snapshot, p)
    play(p, 'g-chain'); choose(p, 'B')
    lines = journal.read_text(encoding='utf-8').splitlines()
    if corruption == 'tamper':
        row = json.loads(lines[0]); row['actor'] = 'italy'; lines[0] = json.dumps(row)
    elif corruption == 'reverse':
        lines.reverse()
    elif corruption == 'duplicate':
        lines.insert(1, lines[0])
    elif corruption == 'missing':
        del lines[1]
    elif corruption == 'tail':
        journal.write_bytes(journal.read_bytes() + b'{"partial":')
    else:
        value = json.loads(snapshot.read_text()); value['payload']['state']['scores']['axis'] = 50
        snapshot.write_text(json.dumps(value))
    if corruption not in ('tail', 'snapshot'):
        journal.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    with pytest.raises(CoreError):
        SaveRepository.recover(snapshot, p.rules, journal)
    if corruption == 'tail':
        restored = SaveRepository.recover(snapshot, p.rules, journal, tolerate_truncated_tail=True)
        assert restored.state.to_dict() == p.state.to_dict()
        with pytest.raises(CoreError):
            OperationLogger(journal).append(p.records[-1])


def test_turn_end_checkpoint_is_automatic_when_configured(tmp_path, data):
    path = tmp_path / 'latest.json'
    p = make(data, checkpoint_path=path)
    initial = json.loads(path.read_text())['payload']['state']['version']
    for _ in range(5):
        p.submit(p.legal_commands(('germany',))[0], ('germany',))
        finish_choices(p)
    saved = SaveRepository.recover(path, p.rules)
    assert saved.state.version > initial
    assert saved.state.turn.country_index == 1
    saved.advance()
    assert saved.state.to_dict() == p.state.to_dict()


def test_hidden_information_and_draw_simulations_do_not_cheat(data, weights):
    p = make(data)
    altered = deepcopy(data)
    next(t for t in altered['initial_tokens'] if t['token_id'] == 'uk-hidden')['definition_id'] = 'uk-other'
    q = make(altered, seed=1000)
    assert observation(p, ('germany',)) == observation(q, ('germany',))
    text = json.dumps(observation(p, ('germany',)))
    assert 'uk-hidden' not in text and 'g-deck-1' not in text and 'uk-secret' not in text
    before = p.state.to_dict()
    ranked_p = OneStepPlanner(p.rules, weights).rank(p, 'germany')
    ranked_q = OneStepPlanner(q.rules, weights).rank(q, 'germany')
    assert ranked_p == ranked_q
    draw = next(r for r in ranked_p if r['command'].get('card') == 'g-draw')
    assert draw['complete']
    assert p.state.to_dict() == before and p.records == []


def test_terminal_victory_outranks_all_heuristics(data, weights):
    data['settings']['victory_margin'] = 10
    weights['card_value_weights']['ability_ready'] = 10**12
    p = make(data)
    candidates = OneStepPlanner(p.rules, weights).rank(p, 'germany')
    assert candidates[0]['command']['card'] == 'g-vp'
    assert candidates[0]['terminal_rank'] == 2


def test_influence_is_explainable_and_garrison_survives_zero_reserves(data, weights):
    data['initial_tokens'] = [t for t in data['initial_tokens'] if not (t['owner_country'] == 'germany' and t['position_id'] == 'germany:RESERVE_ARMY')]
    p = make(data)
    evaluator = ScoreEvaluator(p.rules, weights)
    value = evaluator.evaluate(p, 'germany')
    home = next(s for s in value.score_points if s['site'] == 'g-home')
    assert home['control'] == 1 and home['value'] > 0
    influence = next(i for i in value.token_influence if i['token'] == 'germany-army-0' and i['site'] == 'g-home')
    assert influence['score_influence'] == 1
    assert value.total == value.actual_vp + value.map_value + value.card_value + value.resource_adjustment
    before = p.state.to_dict()
    assert isinstance(evaluator.supply_marginal(p, 'germany', 'germany-navy-0'), float)
    assert p.state.to_dict() == before


def test_simulation_budget_never_scores_partial_card(data, weights):
    weights['max_simulation_nodes'] = 1
    p = make(data)
    candidates = OneStepPlanner(p.rules, weights).rank(p, 'germany')
    chain = next(c for c in candidates if c['command'].get('card') == 'g-chain')
    assert not chain['complete'] and chain['q'] is None


def test_seeded_command_sequences_preserve_invariants_and_replay(tmp_path, data):
    for seed in range(4):
        rng = random.Random(seed)
        journal, snapshot = tmp_path / f'{seed}.jsonl', tmp_path / f'{seed}.json'
        p = make(data, seed=seed, game_id=f'random-{seed}', logger=OperationLogger(journal))
        SaveRepository.save(snapshot, p)
        tokens = set(p.state.tokens)
        for _ in range(45):
            commands = p.legal_commands(TURN_ORDER)
            if not commands:
                break
            start = len(p.records)
            p.submit(rng.choice(commands), TURN_ORDER)
            validate_state(p.rules, p.state)
            for record in p.records[start:]:
                for move in record["outcome"]["changes"]:
                    assert move["token_id"] in tokens
                    assert move["from"] in p.state.positions and move["to"] in p.state.positions
            assert tokens == set(p.state.tokens)
        restored = SaveRepository.recover(snapshot, p.rules, journal)
        assert restored.state.to_dict() == p.state.to_dict()
        assert restored.record_head == p.record_head


def test_failed_atomic_step_keeps_queue_counter_and_records(data):
    p = make(data)
    play(p, 'g-chain', auto_resolve=False)
    before, count, head = p.state.to_dict(), len(p.records), p.record_head
    class Broken:
        def append(self, record):
            raise OSError('I/O failure at operation boundary')
    p.logger = Broken()
    with pytest.raises(OSError):
        p.step()
    assert p.state.to_dict() == before and len(p.records) == count and p.record_head == head


def test_explicit_priority_controls_nested_order_and_ambiguity_fails(data):
    move_initial(data, 'g-status', 'germany:STATUS')
    a = data['abilities'][0]
    a.update(activation_mode='AUTO', discard_cost=0)
    second = deepcopy(a)
    second.update(id='g-second', priority=11, effects=[{'type': 'GAIN_VP', 'amount': 7}])
    data['abilities'].append(second)
    card(data, 'g-status')['abilities'].append('g-second')
    p = make(data)
    play(p, 'g-battle'); choose(p, 'B')
    amounts = [r['outcome']['score_changes'][0]['amount'] for r in p.records if r['operation_type'] == 'GAIN_VP']
    assert amounts == [2, 7]
    parents = [r['parent_resolution_id'] for r in p.records if r['operation_type'] == 'GAIN_VP']
    assert all(parents) and len(set(parents)) == 1
    second['priority'] = 10
    p = make(data)
    play(p, 'g-battle')
    # The announced battle is committed, but the ambiguous battle checkpoint is not.
    with pytest.raises(CoreError, match='Ambiguous'):
        choose(p, 'B')
    validate_state(p.rules, p.state)
    assert not any(r['operation_type'] == 'LAND_BATTLE' for r in p.records)


def test_declared_requirements_and_cost_modifiers_share_legality(data):
    card(data, 'g-vp')['requirements'] = [{'kind': 'occupied', 'region': 'D'}]
    move_initial(data, 'g-status', 'germany:STATUS')
    card(data, 'g-status')['modifiers'] = [{'kind': 'ability_cost', 'ability': 'g-extra', 'amount': -1}]
    p = make(data)
    assert not any(c.get('card') == 'g-vp' for c in p.legal_commands(('germany',)))
    play(p, 'g-battle'); choose(p, 'B'); choose(p, 'use')
    assert not p.state.decision and p.state.scores['axis'] == 2
    assert p.state.tokens['g-copy'].position_id == 'germany:HAND'


def test_unowned_score_source_is_supported_without_token_flags(data):
    source = next(t for t in data['initial_tokens'] if t['token_id'] == 'dynamic-source')
    source.update(owner_country=None, position_id='SCORE_DORMANT')
    p = make(data)
    assert p.state.tokens['dynamic-source'].owner_country is None
    assert 'dynamic-source' not in json.dumps(observation(p, ('germany',)))


@pytest.mark.parametrize('bad', ['modifier', 'trigger', 'number', 'weights'])
def test_unsupported_or_nonfinite_configuration_is_rejected(data, weights, bad):
    if bad == 'modifier':
        card(data, 'g-status')['modifiers'] = [{'kind': 'arbitrary_python'}]
    elif bad == 'trigger':
        data['abilities'][0]['trigger']['unsupported_predicate'] = True
    elif bad == 'number':
        card(data, 'g-vp')['effects'][0]['amount'] = 1.5
    else:
        weights['distance_weights'][0] = float('nan')
        with pytest.raises(CoreError):
            ScoreEvaluator(make(data).rules, weights)
        return
    with pytest.raises(CoreError):
        make(data)


def test_two_builds_recompute_legal_targets_after_first_commit(data):
    card(data, 'g-build')['effects'] *= 2
    p = make(data)
    play(p, 'g-build')
    assert p.state.decision.legal_options == ['B']
    choose(p, 'B')
    assert 'C' in p.state.decision.legal_options
    choose(p, 'C')
    assert p.state.tokens['germany-army-1'].position_id == 'B'
    assert p.state.tokens['germany-army-2'].position_id == 'C'


@pytest.mark.parametrize('attribute,value', [('version', -1), ('rng', 2**32), ('serial', True)])
def test_malformed_state_counters_are_rejected(data, attribute, value):
    p = make(data)
    state = p.state
    setattr(state, attribute, value)
    with pytest.raises(CoreError):
        validate_state(p.rules, state)


def test_naval_construction_and_sea_battle_use_same_pipeline(data):
    card(data, 'g-build')['effects'] = [{'type': 'CHOOSE', 'operation': {'type': 'BUILD_NAVY', 'target': '$choice'}}]
    p = make(data)
    play(p, 'g-build'); choose(p, 'T')
    assert p.state.tokens['germany-navy-1'].position_id == 'T'
    assert any(r['operation_type'] == 'BUILD_NAVY' for r in p.records)
    move_initial(data, 'united-kingdom-navy-0', 'T')
    card(data, 'g-battle')['effects'] = [{'type': 'CHOOSE', 'operation': {'type': 'SEA_BATTLE', 'target': '$choice'}}]
    q = make(data)
    play(q, 'g-battle'); choose(q, 'T')
    assert q.state.tokens['united-kingdom-navy-0'].position_id == 'united-kingdom:RESERVE_NAVY'
    assert any(r['operation_type'] == 'SEA_BATTLE' for r in q.records)


def test_already_collected_vp_is_not_a_future_scoring_opportunity(data, weights):
    data['settings']['max_rounds'] = 1
    p = make(data)
    p.submit(p.legal_commands(('germany',))[0], ('germany',))  # Supply
    before = ScoreEvaluator(p.rules, weights).evaluate(p, 'germany')
    assert next(s for s in before.score_points if s['site'] == 'g-home')['baseline_vp'] == 1
    p.submit(p.legal_commands(('germany',))[0], ('germany',))  # Victory
    after = ScoreEvaluator(p.rules, weights).evaluate(p, 'germany')
    assert after.actual_vp == 1
    assert next(s for s in after.score_points if s['site'] == 'g-home')['baseline_vp'] == 0


def test_existing_journal_cannot_be_reused_by_new_game(tmp_path, data):
    journal = tmp_path / 'shared.jsonl'
    first = make(data, game_id='first', logger=OperationLogger(journal))
    play(first, 'g-chain')
    size = journal.stat().st_size
    second = make(data, game_id='second', logger=OperationLogger(journal))
    before = second.state.to_dict()
    with pytest.raises(CoreError, match='mix journal'):
        play(second, 'g-chain')
    assert second.state.to_dict() == before and second.records == []
    assert journal.stat().st_size == size


def test_external_journal_write_is_detected_before_state_publication(tmp_path, data):
    journal = tmp_path / 'owned.jsonl'
    p = make(data, logger=OperationLogger(journal))
    play(p, 'g-chain', auto_resolve=False)
    before = p.state.to_dict()
    with journal.open('ab') as stream:
        stream.write(b'\n')
    with pytest.raises(CoreError, match='outside its single writer'):
        p.step()
    assert p.state.to_dict() == before
