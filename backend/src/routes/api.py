from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response

from src.config.settings import ARTIFACTS
from src.constants.enums import Role
from src.controllers.query import delivery_status, jobs, recent_activity, require_row, revisions
from src.models.tables import datasets, runs
from src.schemas.requests import Delivery, Import, IntakeTable, Login, PairPreview, PairTables, Review, Run
from src.services.data import PROFILE, csv_records, parse_options, save_revision
from src.services.domain import analyze_tables, sample_rows, sample_shift_tables, sample_tie_shift_tables
from src.services.intake import list_intake, submit_and_maybe_assemble
from src.services.pairing import read_measurement_csv
from src.services.workflow import add_run, cancel_schedule, review_run, schedule_report
from src.utils.exports import write_csv
from src.utils.security import allowed, authenticate, signed_in

router = APIRouter()
analyst = allowed(Role.analyst.value)
reviewer = allowed(Role.reviewer.value)


@router.post('/login')
def login(body: Login):
    return authenticate(body.username, body.password)


@router.get('/me')
def current(account=Depends(signed_in)):
    return dict(account)


@router.get('/profile')
def profile():
    return PROFILE


def download_csv(data, name):
    return Response(data, media_type='text/csv; charset=utf-8',
                    headers={'content-disposition': "attachment; filename*=UTF-8''" + quote(name)})


@router.get('/example.csv')
def example(account=Depends(signed_in)):
    return download_csv(write_csv(sample_rows(), [c['key'] for c in PROFILE['columns']], False), PROFILE['filename'])


@router.get('/example-baseline.csv')
def example_baseline(account=Depends(signed_in)):
    baseline, _ = sample_shift_tables()
    fields = [c['key'] for c in PROFILE.get('baseline_columns', [])] or list(baseline[0])
    return download_csv(write_csv(baseline, fields, False), PROFILE.get('baseline_filename', 'pad_baseline_windows.csv'))


@router.get('/example-trial.csv')
def example_trial(account=Depends(signed_in)):
    _, trial = sample_shift_tables()
    fields = [c['key'] for c in PROFILE.get('trial_columns', [])] or list(trial[0])
    return download_csv(write_csv(trial, fields, False), PROFILE.get('trial_filename', 'pad_trial_windows.csv'))


@router.get('/example-tie-baseline.csv')
def example_tie_baseline(account=Depends(signed_in)):
    baseline, _ = sample_tie_shift_tables()
    fields = [c['key'] for c in PROFILE.get('baseline_columns', [])] or list(baseline[0])
    return download_csv(write_csv(baseline, fields, False), 'pad_tie_baseline_windows.csv')


@router.get('/example-tie-trial.csv')
def example_tie_trial(account=Depends(signed_in)):
    _, trial = sample_tie_shift_tables()
    fields = [c['key'] for c in PROFILE.get('trial_columns', [])] or list(trial[0])
    return download_csv(write_csv(trial, fields, False), 'pad_tie_trial_windows.csv')


@router.get('/datasets')
def list_datasets(account=Depends(signed_in)):
    return revisions()


@router.get('/datasets/{key}')
def get_dataset(key: int, account=Depends(signed_in)):
    dataset = require_row(datasets, key, '数据版本')
    records = dataset.pop('records')
    return {**dataset, 'count': len(records), 'records': records[:100]}


@router.post('/datasets', status_code=201)
def import_data(body: Import, account=Depends(analyst)):
    rows = csv_records(body.csv)
    key, created = save_revision(body.name, rows, account['username'])
    return {'id': key, 'created': created, 'rows': len(rows)}


@router.post('/datasets/pair-tables', status_code=201)
def import_pair_tables(body: PairTables, account=Depends(analyst)):
    """一次交齐两张班表：重叠配对后生成不可变可分析版本。"""
    from src.services.intake import save_pairing_dataset
    baseline_rows = read_measurement_csv(body.baseline_csv)
    trial_rows = read_measurement_csv(body.trial_csv)
    key, created, pairing = save_pairing_dataset(
        body.name, baseline_rows, trial_rows, account['username'])
    return {
        'id': key, 'created': created,
        'baseline_rows': len(baseline_rows), 'trial_rows': len(trial_rows),
        'paired_stations': pairing['paired_station_count'],
        'tie_count': pairing['tie_count'], 'tie_stations': pairing['tie_stations'],
        'unpaired_stations': pairing['unpaired_stations'],
    }


@router.post('/pair-preview')
def pair_preview(body: PairPreview, account=Depends(analyst)):
    """不落库的同步配对试算，供台账页在提交分析前核对两套配法。"""
    options = parse_options(body.options)
    baseline_rows = read_measurement_csv(body.baseline_csv)
    trial_rows = read_measurement_csv(body.trial_csv)
    return analyze_tables(baseline_rows, trial_rows, options)


@router.get('/intake')
def intake_status(account=Depends(signed_in)):
    return list_intake()


@router.post('/intake/{kind}', status_code=201)
def submit_intake(kind: Literal['baseline', 'trial'], body: IntakeTable, account=Depends(analyst)):
    """两班分开交表；同一交接批两张表到齐时自动生成可分析版本。"""
    rows = read_measurement_csv(body.csv)
    result = submit_and_maybe_assemble(
        body.batch_code, kind, body.name or f'{kind}.csv', rows, account['username'])
    return result


@router.get('/runs')
def list_runs(account=Depends(signed_in)):
    return jobs()


@router.post('/runs', status_code=202)
def queue(body: Run, account=Depends(analyst)):
    return add_run(body, account['username'])


@router.get('/runs/{key}')
def result(key: int, account=Depends(signed_in)):
    return require_row(runs, key, '分析任务')


@router.post('/runs/{key}/review')
def review(key: int, body: Review, account=Depends(reviewer)):
    return review_run(key, body, account['username'])


@router.post('/runs/{key}/schedules', status_code=201)
def schedule(key: int, body: Delivery, account=Depends(reviewer)):
    return schedule_report(key, body, account['username'])


@router.get('/delivery')
def delivery(account=Depends(signed_in)):
    return delivery_status()


@router.delete('/schedules/{key}')
def stop_schedule(key: int, account=Depends(reviewer)):
    return cancel_schedule(key, account['username'])


@router.get('/audit')
def activity(account=Depends(signed_in)):
    return recent_activity()


@router.get('/runs/{key}/report.csv')
def report(key: int, account=Depends(signed_in)):
    run = require_row(runs, key, '分析任务')
    if run['decision'] != 'approved' and account['role'] == Role.viewer.value:
        raise HTTPException(403, '只读账号可导出已批准的报告。')
    if not run['result']:
        raise HTTPException(409, '分析结果尚不可导出。')
    path = ARTIFACTS / f"run-{key}-{run['started'].hex()}.csv"
    data = path.read_bytes() if path.exists() else write_csv(run['result']['rows'], run['result'].get('columns'))
    return download_csv(data, f"{PROFILE['title']}-{key}.csv")
