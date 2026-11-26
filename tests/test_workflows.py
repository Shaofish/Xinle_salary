"""Regression checks for user notices and import/export safety. No live DB access."""

import ast
from html.parser import HTMLParser
from datetime import date
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import unquote
from zipfile import ZipFile

import pandas as pd
from flask import Flask

import app as application
from workflow_helpers import insurance_csv, number, remittance_summary, transfer_date, year_month
from workflow_imports import import_insurance, import_history


class Database:
    def __init__(self, rows=None, employee=None, salary=None, fail_write=False):
        self.rows = rows or []
        self.employee = employee
        self.salary = salary
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_write = fail_write

    def cursor(self, *args):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    @property
    def writes(self):
        return [sql for sql, _ in self.executed if sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]


class Cursor:
    def __init__(self, database):
        self.database = database
        self.sql = ''
        self.params = None

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params
        self.database.executed.append((sql, params))
        if self.database.fail_write and sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')):
            raise RuntimeError('simulated database failure')

    def fetchone(self):
        if 'SELECT employee_name FROM employee_info' in self.sql:
            return {'employee_name': '王小明 <測試>'}
        if 'SELECT employee_birth, employee_onboard' in self.sql:
            return self.database.employee
        if 'SELECT `home_caregiver_insurance`' in self.sql:
            return self.database.salary
        if 'SELECT * FROM `employee_salary`' in self.sql:
            return self.database.salary
        if 'SELECT qualification' in self.sql:
            return None if self.params[0] == 'MISSING' else {'qualification': 0, 'subsidy': 0}
        if 'SELECT employee_id FROM employee_salary' in self.sql:
            return {'employee_id': self.params[0]} if self.params[0] == 'EXISTING' else None
        return self.database.employee

    def fetchall(self):
        if self.sql.startswith('DESCRIBE'):
            return [
                {'Field': 'employee_id', 'Type': 'varchar(20)', 'Extra': ''},
                {'Field': 'year_month', 'Type': 'int', 'Extra': ''},
                {'Field': 'holiday_bonus', 'Type': 'int', 'Extra': ''},
                {'Field': 'paid_in_salary', 'Type': 'int', 'Extra': 'STORED GENERATED'},
                {'Field': 'total_payment_insure_grade', 'Type': 'int', 'Extra': ''},
                {'Field': 'Labor_premiums', 'Type': 'int', 'Extra': ''},
                {'Field': 'company_labor', 'Type': 'int', 'Extra': ''},
            ]
        return self.database.rows

    def close(self):
        pass


class ValidationTests(unittest.TestCase):
    def test_invalid_and_nonfinite_numbers_are_not_silently_zero(self):
        for value in ['oops', 'NaN', 'Infinity', '-Infinity']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                number(value, '金額')
        self.assertEqual(number('1,200.0', '金額', integer=True), 1200)

    def test_month_and_date_validation(self):
        self.assertEqual(year_month(202611.0), 202611)
        for value in ['202613', '20261', 'nan']:
            with self.assertRaises(ValueError):
                year_month(value)
        with self.assertRaises(ValueError):
            transfer_date('20260230')
        self.assertEqual(transfer_date('2026-11-30'), '20261130')

    def test_csv_actual_years_and_empty_file(self):
        content = b'range,employee_labor,company_labor,company_occu,company_retire,effective_year\n30000,100,200,30,1800,2025\n31000,110,210,31,1860,2026\n'
        records, _, summary = insurance_csv(content, 2024, 'labor')
        self.assertEqual(summary['years'], [2025, 2026])
        self.assertEqual(summary['count'], 2)
        self.assertEqual(records[0]['range'], 30000)
        with self.assertRaises(ValueError):
            insurance_csv(content.splitlines()[0] + b'\n', 2024, 'labor')

    def test_csv_duplicate_or_partial_rows_rejected(self):
        header = 'range,employee_and0,employee_and1,employee_and2,employee_and3,company_health\n'
        for body in ['30000,100,200,300,400,500\n30000,100,200,300,400,500\n', '30000,,200,300,400,500\n']:
            with self.assertRaises(ValueError):
                insurance_csv((header + body).encode(), 2026, 'health')

    def test_remittance_checks_only_payees_with_positive_payments(self):
        rows = [dict(employee_id='1', employee_name='王小明', employee_card='A123', bank_account='00123', tenth_salary=100, thirtieth_salary=200),
                dict(employee_id='2', employee_name='零元', employee_card='', bank_account='', tenth_salary=0, thirtieth_salary=0)]
        result = remittance_summary(rows, '20261110', '20261130')
        self.assertEqual(result['errors'], [])
        self.assertEqual([b['count'] for b in result['batches']], [1, 1])
        rows[0]['bank_account'] = ''
        self.assertIn('銀行帳號', '\n'.join(remittance_summary(rows, '20261110', '20261130')['errors']))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only'
        self.db = Database()
        mysql = SimpleNamespace(connection=self.db)
        self.labor_content = b'range,employee_labor,company_labor,company_occu,company_retire,effective_year\n30000,100,200,30,1800,2025\n31000,110,210,31,1860,2026\n'
        self.app.add_url_rule('/labor', 'labor_insurance_level_manage', lambda: import_insurance(mysql, 'labor'), methods=['POST'])
        self.app.add_url_rule('/home', 'user', lambda: 'home')
        self.mapping = {'employee_id': '員工編號', 'year_month': '年月', 'holiday_bonus': '三節獎金'}
        self.lookup = lambda *args: {'employee_labor': 100, 'company_labor': 200, 'effective_year': 2026}
        self.app.add_url_rule('/history', 'history', lambda: import_history(mysql, self.mapping, self.lookup, lambda *args: {}), methods=['POST'])
        self.client = self.app.test_client()

    def upload(self, path, content, preview='1', confirmation=None):
        data = {'file': (BytesIO(content), 'data.xlsx' if path == '/history' else 'data.csv'), 'year': '2024', 'preview': preview}
        if confirmation:
            data['confirmation'] = confirmation
        return self.client.post(path, data=data)

    def test_insurance_preview_never_writes_and_commit_targets_actual_years(self):
        preview = self.upload('/labor', self.labor_content).json
        self.assertEqual(preview['summary']['years'], [2025, 2026])
        self.assertEqual(self.db.writes, [])
        done = self.upload('/labor', self.labor_content, '0', preview['token'])
        self.assertEqual(done.status_code, 200)
        deletions = [params for sql, params in self.db.executed if sql.startswith('DELETE')]
        self.assertEqual(deletions, [(2025,), (2026,)])
        self.assertEqual(self.db.commits, 1)

    def test_changed_file_and_missing_confirmation_do_not_write(self):
        self.assertEqual(self.upload('/labor', self.labor_content, '0').status_code, 409)
        preview = self.upload('/labor', self.labor_content).json
        altered = self.labor_content.replace(b'30000', b'32000')
        self.assertEqual(self.upload('/labor', altered, '0', preview['token']).status_code, 409)
        self.assertEqual(self.db.writes, [])

    def test_insurance_write_failure_rolls_back(self):
        token = self.upload('/labor', self.labor_content).json['token']
        self.db.fail_write = True
        response = self.upload('/labor', self.labor_content, '0', token)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.db.commits, 0)
        self.assertEqual(self.db.rollbacks, 1)

    def excel(self, rows):
        output = BytesIO()
        pd.DataFrame(rows).to_excel(output, index=False)
        return output.getvalue()

    def test_history_preview_counts_updates_skips_and_excludes_generated_fields(self):
        content = self.excel([
            {'員工編號': 'EXISTING', '年月': 202611, '三節獎金': 100, 'paid_in_salary': 999},
            {'員工編號': '0001', '年月': 202611, '三節獎金': 200, 'paid_in_salary': 999},
            {'員工編號': 'MISSING', '年月': 202611, '三節獎金': 300, 'paid_in_salary': 999},
        ])
        preview = self.upload('/history', content).json
        self.assertEqual((preview['summary']['new'], preview['summary']['updated'], preview['summary']['skipped']), (1, 1, 1))
        self.assertEqual(self.db.writes, [])
        response = self.upload('/history', content, '0', preview['token'])
        self.assertEqual(response.status_code, 200)
        inserts = [(sql, params) for sql, params in self.db.executed if sql.startswith('INSERT')]
        self.assertEqual(len(inserts), 2)
        self.assertNotIn('paid_in_salary', inserts[0][0])
        self.assertIn('0001', inserts[1][1])
        with self.client.session_transaction() as session:
            self.assertTrue(any('重新列印' in str(message) for _, message in session['_flashes']))

    def test_history_bad_month_and_duplicate_row_cannot_write(self):
        for rows in [
            [{'員工編號': '0001', '年月': 202613}],
            [{'員工編號': '0001', '年月': 202611}, {'員工編號': '0001', '年月': 202611}],
        ]:
            self.assertEqual(self.upload('/history', self.excel(rows)).status_code, 400)
        self.assertEqual(self.db.writes, [])

    def test_history_preview_reports_fallback_insurance_year(self):
        content = self.excel([{'員工編號': '0001', '年月': 202511, 'total_payment_insure_grade': 30000}])
        response = self.upload('/history', content)
        self.assertEqual(response.status_code, 200)
        self.assertIn('2026 年度級距', response.json['summary']['warnings'][0])
        self.assertEqual(self.db.writes, [])

    def test_history_missing_insurance_table_blocks_import(self):
        self.lookup = lambda *args: {}
        content = self.excel([{'員工編號': '0001', '年月': 202611, 'total_payment_insure_grade': 30000}])
        response = self.upload('/history', content)
        self.assertEqual(response.status_code, 400)
        self.assertIn('找不到可用的勞保級距', response.json['error'])
        self.assertEqual(self.db.writes, [])


class AppWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.app = application.app
        self.app.config.update(TESTING=True, SECRET_KEY='test-only')
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['userid'] = 'tester'
        self.info = {'employee_birth': date(1990, 1, 1), 'employee_onboard': date(2020, 1, 1), 'has_single_level_certificate': 0}
        tree = ast.parse(Path(application.__file__).read_text(encoding='utf-8-sig'))
        salary = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'edit_salary_tabs')
        tabs = ast.literal_eval(next(n.value for n in salary.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'fields_by_tab' for t in n.targets)))
        text_fields = {'extras', 'extras2', 'remarks', 'additional_withholding_items', 'additional_withholding2_items'}
        self.saved = {f: '無' if f in text_fields else 0 for fields in tabs.values() for f in fields}
        self.saved['leave_payment_month'] = None
        self.form = {f: '' if value is None else str(value) for f, value in self.saved.items()}
        self.form['year_month'] = '202611'

    def test_salary_add_update_unchanged_and_notice_only_once(self):
        for existing, overrides, title in [(None, {}, '薪資資料已新增'), (self.saved, {'holiday_bonus': '100'}, '薪資資料已更新'), (self.saved, {}, '薪資資料未變更')]:
            with self.subTest(title=title):
                db = Database(employee=self.info, salary=existing)
                with patch.object(application, 'mysql', SimpleNamespace(connection=db)):
                    response = self.client.post('/edit_salary_tabs/E1?year_month=202611', data={**self.form, **overrides}, headers={'X-Workflow-Request': '1'})
                    self.assertTrue(response.json['success'])
                    page = self.client.get(response.json['redirect']).get_data(as_text=True)
                    self.assertIn(title, page)
                    self.assertIn('王小明 &lt;測試&gt;｜2026 年 11 月', page)
                    self.assertIn('salary-success', page)
                    self.assertNotIn('flash-message salary-success', self.client.get('/employee_list').get_data(as_text=True))
                    self.assertEqual(db.commits, 1)

    def test_salary_bad_input_returns_json_without_writes_or_navigation(self):
        for field, value in [('daily_work_hr', 'oops'), ('daily_work_hr', 'NaN'), ('leave_days', '-1')]:
            db = Database(employee=self.info, salary=self.saved)
            with patch.object(application, 'mysql', SimpleNamespace(connection=db)):
                response = self.client.post('/edit_salary_tabs/E1', data={**self.form, field: value}, headers={'X-Workflow-Request': '1'})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json['success'])
                self.assertNotIn('redirect', response.json)
                self.assertEqual(db.writes, [])

    def test_save_failure_does_not_queue_success(self):
        db = Database(employee=self.info, salary=self.saved, fail_write=True)
        with patch.object(application, 'mysql', SimpleNamespace(connection=db)):
            response = self.client.post('/edit_salary_tabs/E1', data=self.form, headers={'X-Workflow-Request': '1'})
        self.assertEqual(response.status_code, 500)
        with self.client.session_transaction() as session:
            self.assertFalse(session.get('_flashes'))

    def test_remittance_preview_and_block_missing_payee_data(self):
        db = Database(rows=[dict(employee_id='1', employee_name='王小明', employee_card='A123', bank_account='', tenth_salary=100, thirtieth_salary=0)])
        with patch.object(application, 'mysql', SimpleNamespace(connection=db)):
            preview = self.client.get('/export_remittance_batch/202611?tenth_date=20261110&thirtieth_date=20261130&preview=1').json
            self.assertFalse(preview['success'])
            self.assertIn('銀行帳號', preview['error'])
            response = self.client.get('/export_remittance_batch/202611?tenth_date=20261110&thirtieth_date=20261130')
            self.assertEqual(response.status_code, 302)
            self.assertNotIn('Content-Disposition', response.headers)
        self.assertEqual(db.writes, [])

    def test_valid_remittance_keeps_bank_account_as_text(self):
        db = Database(rows=[dict(employee_id='1', employee_name='王小明', employee_card='A123', bank_account='0012345', tenth_salary=100, thirtieth_salary=0)])
        with patch.object(application, 'mysql', SimpleNamespace(connection=db)):
            response = self.client.get('/export_remittance_batch/202611?tenth_date=20261110&thirtieth_date=20261130')
        with ZipFile(BytesIO(response.data)) as archive:
            self.assertEqual(len(archive.namelist()), 1)
            with ZipFile(BytesIO(archive.read(archive.namelist()[0]))) as workbook:
                self.assertIn('0012345', workbook.read('xl/sharedStrings.xml').decode())

    def test_pdf_partial_failure_has_ui_summary_and_full_report(self):
        db = Database(rows=[{'員工編號': '1'}, {'員工編號': '2'}])
        with patch.object(application, 'mysql', SimpleNamespace(connection=db)), \
             patch.object(application.pdfkit, 'configuration', return_value=object()), \
             patch.object(application.pdfkit, 'from_string', side_effect=[b'%PDF-test', RuntimeError('conversion failed')]), \
             patch.object(application, 'render_template', return_value='<html>salary</html>'):
            response = self.client.get('/export_pdf_batch/202611')
        summary = json.loads(unquote(response.headers['X-Export-Summary']))
        self.assertEqual(summary['category'], 'warning')
        self.assertIn('成功 1 份，失敗 1 份', summary['message'])
        with ZipFile(BytesIO(response.data)) as archive:
            self.assertIn('匯出結果.txt', archive.namelist())
            self.assertIn('（2）', archive.read('匯出結果.txt').decode('utf-8-sig'))

    def test_all_templates_compile(self):
        for template in Path(application.__file__).parent.joinpath('templates').glob('*.html'):
            self.app.jinja_env.get_template(template.name)

    def test_salary_save_button_is_outside_hidden_tabs(self):
        class Layout(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.submit_ancestors = []

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == 'div':
                    self.stack.append(attributes)
                if tag == 'button' and attributes.get('type') == 'submit':
                    self.submit_ancestors = list(self.stack)

            def handle_endtag(self, tag):
                if tag == 'div' and self.stack:
                    self.stack.pop()

        parser = Layout()
        parser.feed(Path(application.__file__).parent.joinpath('templates/edit_salary_tabs.html').read_text(encoding='utf-8-sig'))
        self.assertFalse(any('tab-content' in a.get('class', '') for a in parser.submit_ancestors))


if __name__ == '__main__':
    unittest.main()
