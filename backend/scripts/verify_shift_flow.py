"""两班交表需求的一键端到端自检（不依赖 PostgreSQL/Redis）。

在 backend 目录运行：
    python scripts/verify_shift_flow.py

会使用临时 SQLite 与临时产物目录，验证：
分开交表到齐才出版本、30 站并列相反判定暂缓、批准被拦、重跑判定不变、
迟到补测另立版本而旧结果不变。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_TMP = tempfile.mkdtemp(prefix='pad-verify-')
os.environ['DATABASE_URL'] = f"sqlite:///{_TMP}/verify.db"
os.environ['SEED_DEMO'] = 'true'
os.environ['JWT_SECRET'] = 'verify-only'
os.environ['ARTIFACTS_DIR'] = f"{_TMP}/artifacts"

from fastapi.testclient import TestClient  # noqa: E402

from src.main import app  # noqa: E402
from src.services.domain import PROTOCOL_ID, sample_tie_shift_tables  # noqa: E402
from src.services.workflow import claim_run  # noqa: E402
from src.worker import calculate  # noqa: E402


def csv_text(rows):
    import csv
    import io
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def main():
    started = time.perf_counter()
    with TestClient(app) as client:
        analyst_token = client.post('/login', json={
            'username': 'analyst', 'password': 'analyst-demo-24'}).json()['token']
        reviewer_token = client.post('/login', json={
            'username': 'reviewer', 'password': 'reviewer-demo-24'}).json()['token']
        headers = {'Authorization': f'Bearer {analyst_token}'}
        review_headers = {'Authorization': f'Bearer {reviewer_token}'}

        baseline, trial = sample_tie_shift_tables(30)
        base_csv, trial_csv = csv_text(baseline), csv_text(trial)

        waiting = client.post('/intake/baseline', headers=headers,
                              json={'batch_code': 'VERIFY', 'csv': base_csv}).json()
        assert waiting['assembly']['complete'] is False
        assert waiting['assembly']['dataset_id'] is None
        print('ok 两班只交原垫表时不生成版本，等待', waiting['assembly']['waiting_for'])

        complete = client.post('/intake/trial', headers=headers,
                               json={'batch_code': 'VERIFY', 'csv': trial_csv}).json()
        dataset_id = complete['assembly']['dataset_id']
        assert complete['assembly']['complete'] and complete['assembly']['tie_count'] == 30
        print('ok 两表到齐生成版本 #%s，并列 30 站' % dataset_id)

        run_id = client.post('/runs', headers=headers,
                             json={'dataset_id': dataset_id, 'options': {'min_pairs': 12}}).json()['id']
        job = claim_run()
        assert job is not None and job[0] == run_id
        calculate(job)
        run = client.get(f'/runs/{run_id}', headers=headers).json()
        result = run['result']
        assert run['state'] == 'completed'
        assert result['tie_conflict'] and not result['can_publish'] and '暂缓' in result['headline']
        assert len(result['pairing_plans']) == 2
        elapsed_ms = (time.perf_counter() - started) * 1000
        print('ok 计算完成：%s；两套配对已列出（累计 %.1f ms）' % (result['headline'], elapsed_ms))
        assert elapsed_ms < 60_000

        blocked = client.post(f'/runs/{run_id}/review', headers=review_headers,
                              json={'decision': 'approved', 'note': '尝试批准'}).status_code
        assert blocked == 409
        print('ok 暂缓结果批准被拦截（409）')

        rerun_id = client.post('/runs', headers=headers,
                               json={'dataset_id': dataset_id, 'options': {'min_pairs': 12}}).json()['id']
        calculate(claim_run())
        rerun = client.get(f'/runs/{rerun_id}', headers=headers).json()['result']
        assert rerun['headline'] == result['headline'] and rerun['can_publish'] is False
        print('ok 重跑原版本判定不变')

        same = client.post('/intake/trial', headers=headers,
                           json={'batch_code': 'VERIFY', 'csv': trial_csv}).json()
        assert same['assembly']['dataset_id'] == dataset_id
        late_csv = trial_csv + f"{PROTOCOL_ID},HX-99,LATE-1,2026-11-13 08:20,2026-11-13 09:20,8.7\n"
        late = client.post('/intake/trial', headers=headers,
                           json={'batch_code': 'VERIFY', 'csv': late_csv}).json()
        new_id = late['assembly']['dataset_id']
        assert new_id and new_id != dataset_id
        old_result = client.get(f'/runs/{run_id}', headers=headers).json()['result']
        assert old_result['headline'] == result['headline']
        print('ok 迟到补测生成新版本 #%s，旧运行结果保持不变' % new_id)

    print('\n全部验证通过，用时 %.2f s' % (time.perf_counter() - started))


if __name__ == '__main__':
    main()
