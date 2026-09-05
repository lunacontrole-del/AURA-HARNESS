from __future__ import annotations
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
MEMORY = ROOT / 'corner_pattern_memory.json'


def main() -> int:
    memory = json.loads(MEMORY.read_text(encoding='utf-8'))
    assert memory.get('memory_version') == 'corner-pattern-memory-v7-six-attachment-curated'
    policy = memory['attachment_curation_policy']
    assert policy['guards']['paper_trade'] is True
    assert policy['guards']['execution_allowed'] is False
    assert 'BRASILEIRAO_2026_AGENTE_IA_TREINAMENTO.py' in policy['rejected_from_training']
    assert 'synthetic_poisson_outcomes' in policy['forbidden_training_sources']
    assert set(memory['windows']) >= {'HT_35_TO_INTERVAL', 'FT_85_TO_END'}

    sys.path.insert(0, str(ROOT))
    from corner_window_specialist import CornerWindowSpecialist
    agent = CornerWindowSpecialist(str(MEMORY))
    sample = {
        'period': '1H', 'minute': "38'", 'data_quality': 0.9,
        'attacks_per_minute': 1.2, 'appm_5min': 0.8, 'appm_10min': 0.6,
        'shots_total': 8, 'pressure': 60, 'blocked_shots': 2,
        'crosses_blocked_last_5m': 1, 'territorial_pressure': 68,
        'temporal_role': 'PRE_ALERT', 'league': 'Brasileirao Serie A'
    }
    result = agent.analyse(sample)
    assert result['window'] == 'HT_35_TO_INTERVAL'
    assert result['mode'] == 'SHADOW_PAPER_TRADE'
    assert result['advisory_only'] is True
    assert result['execution_allowed'] is False
    assert result['decision'] in {'PREPARE', 'OBSERVE', 'NO_BET'}
    print('PASS: independent agent validation')
    print('memory_version:', result['memory_version'])
    print('decision:', result['decision'])
    print('execution_allowed:', result['execution_allowed'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
