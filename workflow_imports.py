"""Two-step import workflows. Previews only read database metadata and rows."""

from io import BytesIO
import pandas as pd
from flask import current_app, flash, jsonify, request, session, url_for

from workflow_helpers import confirmation_token, insurance_csv, number, year_month


def _preview_or_confirm(content, scope, summary):
    token = confirmation_token(content, scope, summary)
    key = 'preview_' + scope
    if request.form.get('preview') == '1':
        session[key] = token
        return jsonify(success=True, token=token, summary=summary)
    if session.get(key) != token or request.form.get('confirmation') != token:
        return jsonify(success=False, error='檔案或影響範圍已變動，請重新預覽並確認後再匯入。'), 409
    return None


def import_insurance(mysql, kind):
    cursor = None
    try:
        upload = request.files.get('file')
        if not upload or not upload.filename:
            raise ValueError('請先選擇 CSV 檔案。')
        content = upload.read()
        records, columns, summary = insurance_csv(content, request.form.get('year'), kind)
        checked = _preview_or_confirm(content, kind, summary)
        if checked is not None:
            return checked
        table = {'labor': 'labor_insurance_level', 'health': 'health_insurance_level'}[kind]
        cursor = mysql.connection.cursor()
        for year in summary['years']:
            cursor.execute(f'DELETE FROM `{table}` WHERE `effective_year` = %s', (year,))
        fields = columns + ['effective_year']
        names = ', '.join(f'`{field}`' for field in fields)
        placeholders = ', '.join(['%s'] * len(fields))
        for record in records:
            cursor.execute(f'INSERT INTO `{table}` ({names}) VALUES ({placeholders})',
                           tuple(record[field] for field in fields))
        mysql.connection.commit()
        session.pop('preview_' + kind, None)
        label = '勞保' if kind == 'labor' else '健保'
        years = '、'.join(map(str, summary['years']))
        flash(f'{label}級距匯入完成：已取代 {years} 年度，共 {summary["count"]} 筆。請核對相關月份薪資中的保費。', 'success')
        return jsonify(success=True, summary=summary,
                       redirect=url_for(f'{kind if kind == "labor" else "health"}_insurance_level_manage', year=summary['years'][-1]))
    except ValueError as error:
        if cursor is not None:
            mysql.connection.rollback()
        return jsonify(success=False, error=str(error)), 400
    except Exception:
        if cursor is not None:
            mysql.connection.rollback()
        current_app.logger.exception('級距匯入失敗')
        return jsonify(success=False, error='級距匯入未完成，請確認檔案欄位及資料庫連線後重試。'), 500
    finally:
        if cursor is not None:
            cursor.close()


def history_plan(content, cursor, mapping, labor_lookup, health_lookup):
    try:
        frame = pd.read_excel(BytesIO(content), dtype={'員工編號': str, 'employee_id': str})
    except Exception:
        raise ValueError('無法讀取 Excel，請確認檔案未損毀；舊版 .xls 可先另存為 .xlsx 再匯入。') from None
    frame.rename(columns={v: k for k, v in mapping.items()}, inplace=True)
    if frame.columns.duplicated().any():
        raise ValueError('Excel 有重複欄位名稱，請修正後再匯入。')
    if not {'employee_id', 'year_month'}.issubset(frame.columns):
        raise ValueError('Excel 必須包含「員工編號」與「年月」欄位。')
    if frame.empty:
        raise ValueError('Excel 沒有薪資資料。')
    cursor.execute('DESCRIBE `employee_salary`')
    schema = {row['Field']: row for row in cursor.fetchall()}
    columns = {name for name, info in schema.items() if 'GENERATED' not in str(info.get('Extra', '')).upper()}
    prepared, skipped, warnings, seen = [], [], [], set()
    new_count, update_count = 0, 0
    for index, row in frame.iterrows():
        label = f'第 {index + 2} 列'
        if row.isna().all():
            skipped.append(label + '：空白列')
            continue
        employee_id = '' if pd.isna(row.get('employee_id')) else str(row['employee_id']).strip()
        if not employee_id:
            raise ValueError(label + '：缺少員工編號。')
        try:
            month = year_month(row.get('year_month'))
        except ValueError as error:
            raise ValueError(f'{label}：{error}') from None
        if (employee_id, month) in seen:
            raise ValueError(f'{label}：檔案內員工 {employee_id} 的 {month} 薪資重複，請先保留一筆。')
        seen.add((employee_id, month))
        cursor.execute('SELECT `qualification`, `subsidy`, `subsidy_none`, `subsidy_half`, `subsidy_full` FROM `employee_info` WHERE `employee_id` = %s', (employee_id,))
        employee = cursor.fetchone()
        if not employee:
            skipped.append(f'{label}：員工 {employee_id} 不存在')
            continue
        data = {}
        for key, value in row.to_dict().items():
            if key not in columns:
                continue
            numeric = str(schema[key].get('Type', '')).lower().startswith(('int', 'bigint', 'smallint', 'tinyint', 'mediumint', 'decimal', 'float', 'double'))
            if pd.isna(value):
                data[key] = 0 if numeric else None
            elif numeric:
                whole = 'int' in str(schema[key].get('Type', '')).lower()
                data[key] = number(value, f'{label} {mapping.get(key, key)}', integer=whole, strict_integer=whole)
            else:
                data[key] = value.item() if hasattr(value, 'item') else value
        data.update(employee_id=employee_id, year_month=month)
        year = month // 100
        for grade_field, lookup, insurance_label in [
                ('total_payment_insure_grade', labor_lookup, '勞保'), ('health_insure_grade', health_lookup, '健保')]:
            grade = number(data.get(grade_field), label + insurance_label + '級距', integer=True, nonnegative=True)
            if not grade:
                continue
            if insurance_label == '勞保':
                result = lookup(cursor, grade, year)
            else:
                result = lookup(cursor, grade, *(employee.get(k, 0) for k in ['qualification', 'subsidy', 'subsidy_none', 'subsidy_half', 'subsidy_full']), year)
            if not result:
                raise ValueError(f'{label}：找不到可用的{insurance_label}級距，請先補齊級距表。')
            actual_year = result.get('effective_year', year)
            if int(actual_year) != year:
                warnings.append(f'{label}：{month} 的{insurance_label}使用 {actual_year} 年度級距，請核對。')
            if insurance_label == '勞保':
                if not number(data.get('Labor_premiums'), label + '勞保費'):
                    data['Labor_premiums'] = result.get('employee_labor', 0)
                for key in ('company_labor', 'company_occu', 'company_retire'):
                    if key in columns:
                        data[key] = result.get(key, 0)
            else:
                if not number(data.get('Insurance_amount'), label + '健保費'):
                    data['Insurance_amount'] = result.get('Insurance_amount', 0)
                if 'company_health' in columns:
                    data['company_health'] = result.get('company_health', 0)
        cursor.execute('SELECT `employee_id` FROM `employee_salary` WHERE `employee_id` = %s AND `year_month` = %s', (employee_id, month))
        if cursor.fetchone():
            update_count += 1
        else:
            new_count += 1
        prepared.append(data)
    if not prepared:
        raise ValueError('沒有可匯入的有效資料。' + '\n' + '\n'.join(skipped[:20]))
    summary = {'new': new_count, 'updated': update_count, 'skipped': len(skipped),
               'skipped_details': skipped, 'warnings': list(dict.fromkeys(warnings))}
    return prepared, summary


def import_history(mysql, mapping, labor_lookup, health_lookup):
    cursor = None
    try:
        upload = request.files.get('file')
        if not upload or not upload.filename:
            raise ValueError('請先選擇 Excel 檔案。')
        content = upload.read()
        cursor = mysql.connection.cursor()
        prepared, summary = history_plan(content, cursor, mapping, labor_lookup, health_lookup)
        checked = _preview_or_confirm(content, 'history', summary)
        if checked is not None:
            return checked
        for data in prepared:
            fields = list(data)
            columns = ', '.join(f'`{field}`' for field in fields)
            placeholders = ', '.join(['%s'] * len(fields))
            updates = ', '.join(f'`{field}` = VALUES(`{field}`)' for field in fields)
            cursor.execute(f'INSERT INTO `employee_salary` ({columns}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}', tuple(data.values()))
        mysql.connection.commit()
        session.pop('preview_history', None)
        flash(f'歷史薪資匯入完成：新增 {summary["new"]} 筆、更新 {summary["updated"]} 筆、跳過 {summary["skipped"]} 筆。', 'success')
        if summary['updated']:
            flash('既有薪資已更新，若先前已列印相關月份薪資單，請重新列印；尚未送交銀行的匯款清冊也請重新產生並核對。', 'warning')
        if summary['skipped']:
            flash('跳過明細：' + '；'.join(summary['skipped_details'][:5]) + ('；其餘請參考匯入前預覽。' if summary['skipped'] > 5 else ''), 'warning')
        if summary['warnings']:
            flash('部分資料使用替代年度級距，請依匯入前預覽核對相關保費。', 'warning')
        return jsonify(success=True, summary=summary, redirect=url_for('user'))
    except ValueError as error:
        if cursor is not None:
            mysql.connection.rollback()
        return jsonify(success=False, error=str(error)), 400
    except Exception:
        if cursor is not None:
            mysql.connection.rollback()
        current_app.logger.exception('歷史薪資匯入失敗')
        return jsonify(success=False, error='歷史薪資匯入未完成，請確認檔案內容與資料庫連線後重試。'), 500
    finally:
        if cursor is not None:
            cursor.close()
