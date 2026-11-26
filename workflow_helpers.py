"""Validation and read-only previews for payroll workflows."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import json
import re

import pandas as pd


def number(value, label, integer=False, nonnegative=False, strict_integer=False):
    text = '' if value is None else str(value).strip().replace(',', '')
    try:
        result = Decimal(text or '0')
    except InvalidOperation:
        raise ValueError(f'{label}：請輸入有效數字。') from None
    if not result.is_finite():
        raise ValueError(f'{label}：請輸入有限的有效數字。')
    if nonnegative and result < 0:
        raise ValueError(f'{label}：不可小於 0。')
    if strict_integer and result != result.to_integral_value():
        raise ValueError(f'{label}：請輸入整數。')
    return int(result) if integer else float(result)


def year_month(value):
    text = str(value).strip()
    if text.endswith('.0'):
        text = text[:-2]
    if not re.fullmatch(r'\d{6}', text):
        raise ValueError('薪資年月請使用六位數格式，例如 202611。')
    try:
        datetime.strptime(text, '%Y%m')
    except ValueError:
        raise ValueError('薪資年月無效，請確認年份與月份。') from None
    return int(text)


def transfer_date(value):
    text = str(value).replace('-', '')
    if not re.fullmatch(r'\d{8}', text):
        raise ValueError('請填寫完整且有效的轉帳日期。')
    try:
        datetime.strptime(text, '%Y%m%d')
    except ValueError:
        raise ValueError('轉帳日期不存在，請重新選擇。') from None
    return text


def confirmation_token(content, scope, summary):
    payload = json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
    return sha256(scope.encode('utf-8') + b'\0' + content + b'\0' + payload).hexdigest()


def insurance_csv(content, fallback_year, kind):
    columns = {
        'labor': ['range', 'employee_labor', 'company_labor', 'company_occu', 'company_retire'],
        'health': ['range', 'employee_and0', 'employee_and1', 'employee_and2', 'employee_and3', 'company_health'],
    }[kind]
    try:
        frame = pd.read_csv(BytesIO(content), encoding='utf-8-sig', dtype=str)
    except Exception:
        raise ValueError('無法讀取 CSV，請確認檔案為 UTF-8 格式且包含標題列。') from None
    frame.columns = [str(c).lower().strip() for c in frame.columns]
    if frame.columns.duplicated().any():
        raise ValueError('CSV 含重複欄位名稱，請修正後再匯入。')
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError('CSV 缺少欄位：' + '、'.join(missing))
    if frame.empty:
        raise ValueError('檔案沒有任何級距資料，未取代既有資料。')
    records, seen = [], set()
    for index, row in frame.iterrows():
        label = f'第 {index + 2} 列'
        raw_year = row['effective_year'] if 'effective_year' in frame.columns else fallback_year
        actual_year = number(raw_year, label + '年度', integer=True, strict_integer=True)
        if not 1900 <= actual_year <= 9999:
            raise ValueError(label + '：請填寫有效的西元年度。')
        record = {'effective_year': actual_year}
        for column in columns:
            if pd.isna(row[column]) or not str(row[column]).strip():
                raise ValueError(f'{label}：{column} 不可空白。')
            record[column] = number(str(row[column]).replace('$', ''), f'{label} {column}',
                                    integer=True, nonnegative=True, strict_integer=True)
        if record['range'] <= 0:
            raise ValueError(label + '：投保級距必須大於 0。')
        key = (actual_year, record['range'])
        if key in seen:
            raise ValueError(f'{label}：同一年度含重複級距 {record["range"]}。')
        seen.add(key)
        records.append(record)
    years = sorted({r['effective_year'] for r in records})
    summary = {'years': years, 'count': len(records),
               'by_year': {str(y): sum(r['effective_year'] == y for r in records) for y in years}}
    return records, columns, summary


def remittance_summary(rows, tenth_date, thirtieth_date):
    dates = [transfer_date(tenth_date), transfer_date(thirtieth_date)]
    errors, batches = [], []
    for field, label, actual_date in zip(['tenth_salary', 'thirtieth_salary'], ['10 日', '30 日'], dates):
        total, count = Decimal(0), 0
        for row in rows:
            employee = f'{row.get("employee_name") or "未填姓名"}（{row.get("employee_id") or "未填編號"}）'
            try:
                amount = Decimal(str(row.get(field) or 0))
            except InvalidOperation:
                errors.append(f'{employee}：{label}匯款金額格式錯誤。')
                continue
            if not amount.is_finite() or amount < 0:
                errors.append(f'{employee}：{label}匯款金額須為非負的有效數字。')
                continue
            if amount == 0:
                continue
            missing = []
            for key, title in [('employee_name', '姓名'), ('employee_card', '身分證字號'), ('bank_account', '銀行帳號')]:
                if not str(row.get(key) or '').strip():
                    missing.append(title)
            account = str(row.get('bank_account') or '').strip()
            if account and not re.fullmatch(r'[0-9]+', account):
                missing.append('銀行帳號格式（僅限數字）')
            if missing:
                errors.append(f'{employee}：請補齊或修正' + '、'.join(missing) + '。')
            count += 1
            total += amount
        batches.append({'label': label, 'date': actual_date, 'count': count, 'total': f'{total:,.2f}'})
    return {'batches': batches, 'errors': list(dict.fromkeys(errors))}
