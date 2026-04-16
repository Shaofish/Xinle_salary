from flask import Flask, render_template, request, redirect, url_for, session, flash ,send_file, jsonify
from flask_mysqldb import MySQL
from MySQLdb.cursors import DictCursor
from MySQLdb import IntegrityError
from datetime import datetime, timedelta, date
import pdfkit  # 加在檔案開頭
from flask import make_response
import pandas as pd
import io
from io import BytesIO
import pymysql
from functools import wraps
import json
from dateutil.relativedelta import relativedelta
from urllib.parse import quote
import numpy as np
import MySQLdb.cursors

app = Flask(__name__)

# ====== MySQL 設定 ======
app.config['MYSQL_HOST'] = '127.0.0.1'
app.config['MYSQL_USER'] = 'root'
app.config['MYSQL_PASSWORD'] = 'gtololol01314'  # 請替換為你的 MySQL 密碼
app.config['MYSQL_DB'] = 'salary_db'  # 更改為要連接的資料庫名稱
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'

mysql = MySQL(app)
app.secret_key = 'your_secret_key'

# ====== 登入驗證裝飾器 ======
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'userid' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ====== 自定義函數 ======
def get_all_available_months(employee_id):
    cursor = mysql.connection.cursor()
    try:
        cursor.execute("SELECT `employee_onboard` FROM `employee_info` WHERE `employee_id` = %s", (employee_id,))
        result = cursor.fetchone()
    finally:
        cursor.close()

    # 2. 設定起始日期 (Start Date)
    if result and result['employee_onboard']:
        # 確保格式為 date 物件 (有些資料庫驅動回傳的是 datetime)
        onboard_date = result['employee_onboard']
        if isinstance(onboard_date, datetime):
            onboard_date = onboard_date.date()
        
        # 將起始日設為該月的 1 號 (方便後續迴圈計算)
        start_date = onboard_date.replace(day=1)
    else:
        # 防呆：如果找不到入職日，預設從今天開始
        start_date = date.today().replace(day=1)

    # 3. 設定結束日期 (End Date) = 當前時間往後一年
    today = date.today()
    target_date = today + relativedelta(years=1)
    # 同樣設為該月 1 號以進行比較
    end_date = target_date.replace(day=1)

    # 4. 生成月份列表
    all_months = []
    current_date = start_date

    # 迴圈：只要 當前日期 <= 結束日期，就加入列表
    while current_date <= end_date:
        # 轉成 YYYYMM 格式 (int)
        month_int = int(current_date.strftime("%Y%m"))
        all_months.append(month_int)
        
        # 往後推一個月
        current_date += relativedelta(months=1)

    # 5. 回傳 (反序排列：最新的月份在上面)
    return sorted(all_months, reverse=True)


def get_latest_salary(employee_id):
    cur = mysql.connection.cursor()
    cur.execute(
        "SELECT * FROM `employee_salary` WHERE `employee_id` = %s ORDER BY `year_month` DESC LIMIT 1",
        (employee_id,))
    salary = cur.fetchone()
    cur.close()
    return salary

def log_edit(userid, employee_id, action_type, changed_fields: dict):
    cur = mysql.connection.cursor()
    cur.execute("""
        INSERT INTO edit_log (userid, employee_id, action_type, changed_fields)
        VALUES (%s, %s, %s, %s)
    """, (userid, employee_id, action_type, json.dumps(changed_fields, ensure_ascii=False)))
    mysql.connection.commit()
    cur.close()

def calculate_birthday_bonus(onboard_date_str, birth_str, salary_month=None):
    # 支援 yyyy-mm-dd 或 yyyy/mm/dd 或 yyyymmdd
    def parse_date(date_str):
        if not date_str:
            return None
        date_str = str(date_str).strip().replace('－', '-').replace('–', '-').replace('\u200b', '').replace('\xa0', '')
        for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y%m%d'):
            try:
                return datetime.strptime(date_str, fmt)
            except Exception:
                continue
        return None

    onboard_date = parse_date(onboard_date_str)
    birth_date = parse_date(birth_str)

    if not onboard_date or not birth_date:
        return 0

    # 新增: 支援傳入薪資年月 (yyyymm)
    if salary_month:
        try:
            salary_date = datetime.strptime(str(salary_month), '%Y%m')
        except Exception:
            salary_date = datetime.today()
    else:
        salary_date = datetime.today()

    # 檢查該月是否生日月
    if salary_date.month != birth_date.month:
        return 0

    # 計算年資（月數）
    diff = relativedelta(salary_date, onboard_date)
    months = diff.years * 12 + diff.months
    if months >= 12:
        return 1000
    elif months >= 6:
        return 500
    else:
        return 0
    
def calculate_leave_days(onboard_date_input, selected_dt):
    # 轉成 datetime
    if isinstance(onboard_date_input, str):
        onboard_date = datetime.strptime(onboard_date_input, "%Y-%m-%d")
    elif isinstance(onboard_date_input, datetime):
        onboard_date = onboard_date_input
    else:  # 如果是 date
        onboard_date = datetime.combine(onboard_date_input, datetime.min.time())

    # 用選擇的年月計算年資
    diff = relativedelta(selected_dt, onboard_date)
    years = diff.years
    months = diff.months

    if years == 0 and months < 6:
        return 0
    elif years == 0 and months >= 6:
        return 3
    elif years == 1:
        if months < 6:
            return 10
        else:
            return 7
    elif years == 2:
        return 10
    elif 3 <= years < 5:
        return 14
    elif 5 <= years < 10:
        return 15
    elif years >= 10:
        return min(15 + (years - 9), 30)
    else:
        return 0
    
def safe_int(value):
    try:
        if not value: return 0  # 處理 None 或 空字串
        return int(float(value)) # 先轉 float 再轉 int，避免 "100.0" 轉 int 報錯
    except (ValueError, TypeError):
        return 0

def safe_float(value):
    try:
        if not value: return 0.0
        return float(value)
    except (ValueError, TypeError):
        return 0.0
    
# ====== 欄位名稱中英對照（可自行增減）======
column_mapping = {
    'year_month': '年月',
    'total_hr': '總工時',
    'daily_work_salary': '本薪',
    'Salary_subtotal': '薪資小計',
    'Split_amount': '拆帳金額',
    'tenth_salary': '10日薪資',
    'thirtieth_salary': '30日薪資',
    'total_welfare': '福利總額',
    'subtotal_withholding': '扣項小計',
    'payroll_due': '應發薪資',
    'paid_in_salary': '實領薪資',
    'remarks': '備註',
    'employment_status': '在職狀態',
    'main_total__overtime_pay': '加班費總計',
    'labor_insurance_grade' : '勞保級距',
    'company_total' : '公司總負擔額',
    'BGA_view' : 'B+G+A碼(觀看用)',
    'ultimate_salary' : '薪資總計',
    #個人資料
    'employee_id': '員工編號',
    'employee_birth': '出生日期',
    'employee_card': '身份證字號',
    'employee_name': '姓名',
    'employee_onboard': '到職日',
    'subsidy_full': '全額補助人數',
    'subsidy_half': '半額補助人數',
    'subsidy_none': '無補助人數',
    'status': '在職狀態',
    'qualification': '本人投保身分',
    'subsidy': '本人補助身分',
    
    
    #約定薪資結構
    'capacity_amout': '產能金額',
    'AA09_bonus_view' : 'A碼鼓勵獎金(顯示用)',
    'daily_work_hr': '平日時數',
    'holiday_work_hr': '假日時數',
    'transition_hr': '轉場時數',
    'transition_allowance': '轉場津貼',
    'Overtime_pay_rest_days12hr': '休息日加班時數(1-2)',
    'Overtime_pay_rest_days38hr': '休息日加班時數(3-8)',
    'total_overtime_pay_restdays' : '休息日總加班費',
    'Overtime_pay_weekdays910hr': '平日加班時數(9-10)',
    'Overtime_pay_weekdays1112hr': '平日加班時數(11-12)',
    'Overtime_pay_weekdays910': '平日加班費(9-10)',
    'Overtime_pay_weekdays1112': '平日加班費(11-12)',
    'total_overtime_pay_weekdays': '平日總加班費',
    'Performance_bonuses': '績效獎金',
    'Overtime_pay_rest_days12': '休息日加班費(1-2)',
    'Overtime_pay_rest_days38': '休息日加班費(3-8)',
    'holiday_overtime_hr': '例假日班加班總時數',
    'total_pay_holidays': '例假日班加班費總金額',
    'National_holidays_hr': '國定假日時數',
    'total_pay_national_holidays': '國定假日加班費總金額',
    'AA09_bonus': 'A碼鼓勵獎金',
    'Inter_district_subsidie': '跨區補助',
    #非固定支付項目
    'leave_days': '特休天數',
    'leave_taken_days': '特休請假天數',
    'annual_total_hours': '年度總工時',
    'leave_hours_per_day': '特休一天抵扣年時數',
    'total_service_hours': '總服務時數',
    'leave_pay_days': '特休給付天數',
    'leave_deduction_amount': '特休折抵金額',
    'leave_payment_month_of_year': '每年特休發放月份',
    'leave_payment_month': '特休發放月份',
    'duty_allowance': '行政薪資/職務津貼',
    #福利設定
    'licence_allowance': '證照津貼',
    'holiday_bonus': '三節獎金',
    'birthday_bonus': '生日禮金',
    'referral_fee': '介紹費',
    'travel_allowance': '旅遊津貼',
    'check_up': '體檢補助',
    'training_fee': '教育訓練費',
    'new_case_allowance': '開發新案獎金',
    'teaching_subsidy': '教學補助費',
    'extras': '其他項目 1',
    'extras_amout': '其他金額 1',
    'extras2': '其他項目 2',
    'extras2_amount': '其他金額 2',
    #應代扣項目
    'total_payment_insure_grade': '勞保級距',
    'health_insure_grade': '健保級距',
    'health_insurance_grade': '健保級距',
    'Labor_premiums': '勞保費',
    'Insurance_amount': '健保費',
    'company_labor': '公司勞保費',
    'company_occu': '公司職災費',
    'company_health': '公司健保費', 
    'company_retire': '公司退休金',
    'retirement_plan_withdraw': '勞退自提',
    'home_caregiver_insurance': '照服員險',
    'group_accident_insurance': '團體意外險',
    'additional_withholding_items': '額外代扣項目 1',
    'additional_withholding_amout': '額外代扣金額 1',
    'additional_withholding2_items': '額外代扣項目 2',
    'additional_withholding2_amount': '額外代扣金額 2',
}
# ====== 中文對應（操作類型）======
action_mapping = {
    'edit_employee': '編輯個人資料',
    'edit_salary': '編輯薪資',
    'add_salary': '新增薪資',
}

# ====== 登入功能 ======
@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        userid = request.form['userid']
        password = request.form['password']

        cursor = mysql.connection.cursor()
        cursor.execute("SELECT * FROM `user` WHERE `userid` = %s", (userid,))
        user = cursor.fetchone()
        cursor.close()

        if user:
            if user['password'] == password:
                session['userid'] = user['userid']
                session['login_success'] = True
                return redirect(url_for('user'))
            else:
                flash('密碼錯誤', 'danger')
        else:
            flash('用戶不存在', 'danger')

    return render_template('login.html')

# ====== 使用者主頁 ======
@app.route('/user')
@login_required
def user():
    if session.pop('login_success', None):
        flash("登入成功", "success")
    return render_template('user.html')

# ===== 員工管理頁面 =====
@app.route('/employee_manage')
@login_required
def employee_manage():
    cursor = mysql.connection.cursor()

    # 在職員工
    cursor.execute("""
        SELECT employee_id, employee_name 
        FROM employee_info 
        WHERE is_hidden = FALSE AND (status = 'active' OR status IS NULL)
    """)
    employees = cursor.fetchall()

    # 已離職員工
    cursor.execute("""
        SELECT employee_id, employee_name, resign_date
        FROM employee_info 
        WHERE is_hidden = FALSE AND status = 'resigned'
    """)
    resigned_employees = cursor.fetchall()

    cursor.close()

    return render_template('employee_manage.html',
                            employees=employees,
                            resigned_employees=resigned_employees)



@app.route('/add_employee', methods=['GET', 'POST'])
@login_required
def add_employee():
    cursor = mysql.connection.cursor()
    if request.method == 'POST':
        employee_id = request.form['employee_id']
        employee_birth = request.form['employee_birth']
        employee_card = request.form['employee_card']
        employee_name = request.form['employee_name']
        employee_onboard = request.form['employee_onboard']
        # 新增級距欄位
        labor_grade = request.form.get('labor_insurance_grade', 0)
        health_grade = request.form.get('health_insurance_grade', 0)
        
        qualification = int(request.form.get('qualification', 0))
        subsidy = int(request.form.get('subsidy', 0))
        subsidy_none = int(request.form.get('subsidy_none', 0))
        subsidy_half = int(request.form.get('subsidy_half', 0))
        subsidy_full = int(request.form.get('subsidy_full', 0))
        leave_payment_month_of_year = request.form.get('leave_payment_month_of_year', None)
        if leave_payment_month_of_year == '': leave_payment_month_of_year = None

        try:
            cursor.execute("""
                INSERT INTO `employee_info` 
                (`employee_id`, `employee_birth`, `employee_card`, `employee_name`, 
                 `employee_onboard`, `qualification`, `subsidy`, 
                 `subsidy_none`, `subsidy_half`, `subsidy_full`, 
                 `leave_payment_month_of_year`, `labor_insurance_grade`, `health_insurance_grade`)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (employee_id, employee_birth, employee_card, employee_name,
                  employee_onboard, qualification, subsidy,
                  subsidy_none, subsidy_half, subsidy_full, 
                  leave_payment_month_of_year, labor_grade, health_grade))
            mysql.connection.commit()
            flash("成功新增員工資料", "success")
            return redirect(url_for('employee_manage'))
        except IntegrityError:
            flash("資料庫錯誤（請檢查ID是否重複）", "error")
            return render_template('add_employee.html', data=request.form)
    
    # GET: 獲取級距列表供前端選單使用
    cursor.execute("SELECT * FROM labor_insurance_level ORDER BY `range` ASC")
    labor_levels = cursor.fetchall()
    cursor.execute("SELECT * FROM health_insurance_level ORDER BY `Range` ASC")
    health_levels = cursor.fetchall()
    cursor.close()
    
    return render_template('add_employee.html', data={}, labor_levels=labor_levels, health_levels=health_levels)


@app.route('/edit_profile/<employee_id>', methods=['GET', 'POST'])
@login_required
def edit_profile(employee_id):
    if request.method == 'POST':
        # 1. 安全接收資料 (使用 .get 避免 KeyError)
        new_employee_id = request.form.get('employee_id')
        employee_name = request.form.get('employee_name')
        employee_birth = request.form.get('employee_birth')
        employee_card = request.form.get('employee_card')
        employee_onboard = request.form.get('employee_onboard')
        
        # 處理數值 (防止空字串導致 int() 轉換失敗)
        def safe_int(val):
            return int(val) if val and val.strip() else 0

        labor_grade = request.form.get('labor_insurance_grade') # 這裡不轉 int，因為可能是字串或特定格式
        health_grade = request.form.get('health_insurance_grade')
        
        qualification = safe_int(request.form.get('qualification'))
        subsidy = safe_int(request.form.get('subsidy'))
        subsidy_none = safe_int(request.form.get('subsidy_none'))
        subsidy_half = safe_int(request.form.get('subsidy_half'))
        subsidy_full = safe_int(request.form.get('subsidy_full'))
        # 檢查眷口補助人數總和是否等於投保眷口數
        total_subsidy_dependents = subsidy_none + subsidy_half + subsidy_full
        if qualification != total_subsidy_dependents:
            # 準備要顯示的文字 (使用 \\n 來讓彈出視窗的文字換行，看起來更清楚)
            error_msg = f"❌ 更新失敗：健保眷口設定不符！\\n\\n您選擇投保「{qualification} 名眷口」\\n但各項補助人數相加為 {total_subsidy_dependents} 人。"
            
            # 直接回傳 JavaScript：彈出警告，並退回上一頁 (保留使用者輸入的資料)
            return f"<script>alert('{error_msg}'); window.history.back();</script>"
        
        leave_month_input = request.form.get('leave_payment_month_of_year')
        if not leave_month_input or leave_month_input.strip() == "":
            leave_payment_month_of_year = 1  # 預設為 1 月
        else:
            leave_payment_month_of_year = int(leave_month_input)

        # 使用 with 語法自動管理 cursor 關閉
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            # 2. 檢查新 ID 是否與其他現有員工重複 (排除自己)
            if new_employee_id != employee_id:
                cursor.execute("SELECT employee_id FROM employee_info WHERE employee_id = %s", (new_employee_id,))
                if cursor.fetchone():
                    flash(f'錯誤：員工編號 {new_employee_id} 已存在，請使用其他編號。', 'danger')
                    return redirect(url_for('edit_profile', employee_id=employee_id))

            # 3. 取得原始資料用於 Log (使用原本的 ID)
            cursor.execute("SELECT * FROM employee_info WHERE employee_id = %s", (employee_id,))
            original = cursor.fetchone()
            
            if not original:
                flash('錯誤：找不到原始員工資料', 'danger')
                return redirect(url_for('employee_manage'))

            # 4. 開始修改資料 (包含主鍵修改的特殊處理)
            # 暫時關閉外鍵檢查，允許修改 Parent Table 的 ID
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")

            # Update 主表
            cursor.execute("""
                UPDATE `employee_info`
                SET `employee_id` = %s, `employee_name` = %s, `employee_birth` = %s, `employee_card` = %s, 
                    `employee_onboard` = %s, `qualification` = %s, `subsidy` = %s, 
                    `subsidy_none` = %s, `subsidy_half` = %s, `subsidy_full` = %s, 
                    `leave_payment_month_of_year` = %s,
                    `labor_insurance_grade` = %s, `health_insurance_grade` = %s
                WHERE `employee_id` = %s
            """, (new_employee_id, employee_name, employee_birth, employee_card, 
                  employee_onboard, qualification, subsidy, subsidy_none, subsidy_half, subsidy_full, 
                  leave_payment_month_of_year, labor_grade, health_grade, employee_id))

            # Update 關聯表 (手動 Cascade)
            # 注意：這裡的 WHERE 條件必須用 `employee_id` (舊 ID)，但因為我們關閉了外鍵檢查，且第一步已經改了主表，
            # 這裡其實有點風險。比較穩健的做法是：如果 ID 有變，才執行這些。
            
            if new_employee_id != employee_id:
                # 更新其他關聯表
                cursor.execute("UPDATE `employee_salary` SET `employee_id` = %s WHERE `employee_id` = %s", (new_employee_id, employee_id))
                cursor.execute("UPDATE `edit_log` SET `employee_id` = %s WHERE `employee_id` = %s", (new_employee_id, employee_id))
                # 可以在此加入其他關聯表

            # 恢復外鍵檢查
            cursor.execute("SET FOREIGN_KEY_CHECKS=1")
            
            # 提交交易
            mysql.connection.commit()

            # 5. Log 紀錄
            changed = {}
            compare_fields = {
                'employee_id': new_employee_id, 
                'employee_name': employee_name, 
                'employee_birth': employee_birth, 
                'employee_card': employee_card,
                'employee_onboard': employee_onboard,
                'qualification': qualification,
                'subsidy': subsidy,
                'subsidy_none': subsidy_none,
                'subsidy_half': subsidy_half,
                'subsidy_full': subsidy_full,
                'leave_payment_month_of_year': leave_payment_month_of_year,
                'labor_insurance_grade': labor_grade,
                'health_insurance_grade': health_grade
            }
            
            # 特別處理：比對 ID 時，原始資料要拿 'employee_id'，新資料是 new_employee_id
            if original['employee_id'] != new_employee_id:
                 changed['employee_id'] = {"old": original['employee_id'], "new": new_employee_id}

            for field, new_value in compare_fields.items():
                if field == 'employee_id': continue # 已經處理過
                old_value = original.get(field)
                # 轉成字串比對比較保險，避免 None vs '' 或 int vs str 的問題
                if str(old_value if old_value is not None else '') != str(new_value if new_value is not None else ''):
                    changed[field] = {"old": old_value, "new": new_value}

            if changed:
                # 確保 log_edit 函式存在且參數正確
                try:
                    log_edit(session.get('userid'), new_employee_id, 'edit_employee', changed)
                except Exception as e:
                    print(f"Log 寫入失敗: {e}") # 避免 Log 錯誤導致整個流程看起來像失敗

            flash('員工資料已成功更新', 'success')
            return redirect(url_for('employee_manage'))

        except Exception as e:
            mysql.connection.rollback()
            # 將錯誤印在 Console 方便除錯
            print(f"Update Error: {e}")
            flash(f'更新失敗: {str(e)}', 'danger')
            return redirect(url_for('edit_profile', employee_id=employee_id))
        
        finally:
            cursor.close()

    # --- GET request 處理 ---
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute("SELECT * FROM labor_insurance_level ORDER BY `range` ASC")
        labor_levels = cursor.fetchall()
        
        cursor.execute("SELECT * FROM health_insurance_level ORDER BY `Range` ASC")
        health_levels = cursor.fetchall()
        
        cursor.execute("SELECT * FROM `employee_info` WHERE `employee_id` = %s", (employee_id,))
        employee = cursor.fetchone()

        if not employee:
            flash('找不到該員工資料', 'danger')
            return redirect(url_for('employee_manage'))

        return render_template('edit_profile.html', employee=employee, labor_levels=labor_levels, health_levels=health_levels)
    finally:
        cursor.close()

# ====== 修改 edit_salary_tabs 預設級距邏輯 ======
# 在原本的 GET 區塊尋找 if not salary 處修改：
    if not salary:
        salary = {'year_month': selected_month}
        # 預設帶入基本資料中設定的級距
        salary['total_payment_insure_grade'] = info.get('labor_insurance_grade', 0)
        salary['health_insure_grade'] = info.get('health_insurance_grade', 0)
        for field in all_fields:
            if field not in salary: salary[field] = 0


# ====== 員工列表 ======
@app.route('/employee_list')
@login_required
def employee_list():
    if session.pop('salary_saved', None):
        flash("薪資資料新增成功", "success")

    cursor = mysql.connection.cursor()

    # 在職員工
    cursor.execute("""
        SELECT employee_id, employee_name 
        FROM employee_info 
        WHERE is_hidden = FALSE AND (status = 'active' OR status IS NULL)
    """)
    employees = cursor.fetchall()

    # 已離職員工
    cursor.execute("""
        SELECT employee_id, employee_name, resign_date
        FROM employee_info 
        WHERE is_hidden = FALSE AND status = 'resigned'
    """)
    resigned_employees = cursor.fetchall()

    cursor.close()

    return render_template('employee_list.html',
                            employees=employees,
                            resigned_employees=resigned_employees)

# ====== 離職員工 ======
@app.route('/resign_employee/<employee_id>', methods=['POST'])
@login_required
def resign_employee(employee_id):
    cursor = mysql.connection.cursor()
    # 取得原始資料用於紀錄 (可選)
    cursor.execute("SELECT employee_name FROM employee_info WHERE employee_id = %s", (employee_id,))
    emp = cursor.fetchone()
    
    # 執行更新
    cursor.execute("""
        UPDATE employee_info 
        SET status = 'resigned', resign_date = NOW()
        WHERE employee_id = %s
    """, (employee_id,))
    
    # ✨ 關鍵：手動建立一筆「員工離職」專屬紀錄
    log_edit(session['userid'], employee_id, '員工離職', {
        "人事異動": {"old": "在職", "new": "已離職"},
        "異動時間": {"old": "無", "new": datetime.now().strftime('%Y-%m-%d %H:%M')}
    })
    
    mysql.connection.commit()
    cursor.close()
    flash(f"員工 {emp['employee_name']} 已標記為離職", "info")
    return redirect(url_for('employee_manage'))

# ====== 員工復職路由修改 ======
@app.route('/reinstate/<employee_id>', methods=['POST'])
@login_required
def reinstate_employee(employee_id):
    cursor = mysql.connection.cursor()
    
    # 執行更新
    cursor.execute("""
        UPDATE employee_info
        SET status = 'active', rehire_date = NOW(), resign_date = NULL
        WHERE employee_id = %s
    """, (employee_id,))
    
    # ✨ 關鍵：手動建立一筆「員工復職」專屬紀錄
    log_edit(session['userid'], employee_id, '員工復職', {
        "人事異動": {"old": "離職", "new": "復職(在職)"},
        "異動時間": {"old": "無", "new": datetime.now().strftime('%Y-%m-%d %H:%M')}
    })
    
    mysql.connection.commit()
    cursor.close()
    flash("該員工已成功復職，請修改該員工的到職日期以及其他個人資料", "success")
    return redirect(url_for('edit_profile', employee_id=employee_id))

# ====== 編輯員工基本資料 ======
@app.route('/edit_salary_tabs/<employee_id>', methods=['GET', 'POST'])
@login_required
def edit_salary_tabs(employee_id):
    # 分頁欄位（新增 leave_payment_month）
    fields_by_tab = {
        'salary': [
            'capacity_amout', 'Split_amount', 'AA09_bonus_view', 'daily_work_hr', 'holiday_work_hr', 'transition_hr', 
            'transition_allowance', 'Overtime_pay_rest_days12hr', 'Overtime_pay_rest_days38hr', 'total_overtime_pay_restdays',
            'Overtime_pay_weekdays910hr', 'Overtime_pay_weekdays1112hr', 'Overtime_pay_weekdays910', 'Overtime_pay_weekdays1112',
            'total_overtime_pay_weekdays', 'Performance_bonuses', 'Overtime_pay_rest_days12', 'Overtime_pay_rest_days38',
            'holiday_overtime_hr', 'total_pay_holidays', 'National_holidays_hr', 'total_pay_national_holidays', 'AA09_bonus',
            'Inter_district_subsidie', 'duty_allowance'
        ],
        'other': [
            'leave_days', 'leave_taken_days', 'annual_total_hours', 'leave_hours_per_day', 'total_service_hours',
            'leave_pay_days', 'leave_deduction_amount', 'leave_payment_month'  # ✨ 新增
        ],
        'bonus': [
            'licence_allowance', 'holiday_bonus', 'birthday_bonus', 'referral_fee', 'travel_allowance', 'check_up',
            'training_fee', 'teaching_subsidy', 'new_case_allowance', 'extras', 'extras_amout','extras2','extras2_amount'
        ],
        'insurance': [
            'total_payment_insure_grade', 'health_insure_grade', 'Labor_premiums', 'Insurance_amount', 'retirement_plan_withdraw',
            'home_caregiver_insurance', 'group_accident_insurance', 'additional_withholding_items', 'additional_withholding_amout',
            'company_labor', 'company_occu', 'company_health', 'company_retire', 'company_total','additional_withholding2_items', 'additional_withholding2_amount','remarks'
        ]
    }

    all_fields = [item for sublist in fields_by_tab.values() for item in sublist]
    cur = mysql.connection.cursor()
    
    cursor = mysql.connection.cursor()
    cursor.execute(
        "SELECT employee_name FROM employee_info WHERE employee_id = %s",
        (employee_id,)
    )
    name_result = cursor.fetchone()
    employee_name = name_result['employee_name'] if name_result else employee_id
    cursor.close()

    # === 取得員工資訊（包含補助資訊）===
    cursor = mysql.connection.cursor()
    cursor.execute(
        """SELECT employee_birth, employee_onboard, qualification, subsidy, 
                  subsidy_none, subsidy_half, subsidy_full, leave_payment_month_of_year,
                  labor_insurance_grade, health_insurance_grade
           FROM employee_info 
           WHERE employee_id = %s""",
        (employee_id,)
    )
    info = cursor.fetchone()
    cursor.close()

    if not info:
        flash("找不到該員工資料", "danger")
        return redirect(url_for('employee_list'))

    # === GET/POST 共用: 確定當前處理月份 ===
    selected_month = request.args.get('year_month') or request.form.get('year_month')
    selected_month = int(selected_month) if selected_month else int(datetime.today().strftime('%Y%m'))
    selected_dt = datetime.strptime(str(selected_month), "%Y%m")
    
    # 計算到職滿一年的日期
    onboard_date = info['employee_onboard']
    if isinstance(onboard_date, date):
        onboard_date = datetime.combine(onboard_date, datetime.min.time())
    
    one_year_anniversary = onboard_date + relativedelta(years=1)
    one_year_month = int(one_year_anniversary.strftime('%Y%m'))
    
    # 判斷是否已過到職滿一年
    is_past_one_year = selected_month >= one_year_month

    # === POST 儲存薪資資料 ===
    if request.method == 'POST':
        year_month = int(request.form['year_month'])
        birthday_bonus = calculate_birthday_bonus(
            info['employee_onboard'], info['employee_birth'], year_month
        )

        # 收集表單資料 (使用 safe_int/float 防止報錯)
        values_dict = {}
        
        for field in all_fields:
            val = request.form.get(field)
            
            # 特殊欄位處理
            if field == 'birthday_bonus':
                values_dict[field] = birthday_bonus # 這是後端算的，直接用
            
            elif field == 'leave_payment_month':
                # 特休發放月：如果是空值，存成 None (NULL)，否則存 int
                if val and val.strip():
                    values_dict[field] = safe_int(val)
                else:
                    values_dict[field] = None
            
            elif field in ['additional_withholding_items']: # 文字欄位直接存
                 values_dict[field] = val if val else ''
            
            else: # 其他預設都是數值欄位
                # 判斷是否為浮點數欄位 (例如工時、獎金、金額)
                if 'hr' in field or 'bonus' in field or 'amount' in field or 'amout' in field:
                     values_dict[field] = safe_float(val)
                else:
                     values_dict[field] = safe_int(val)
        
        # 將績效獎金改為非負
        try:
            perf_bonus = float(values_dict.get('Performance_bonuses', 0))
            if perf_bonus < 0:
                perf_bonus = 0
        except ValueError:
            perf_bonus = 0
        values_dict['Performance_bonuses'] = perf_bonus

        # === 計算補助條件 ===
        today = datetime.today().date()
        one_year_passed = (today - info['employee_onboard']).days >= 365

        # 近三個月服務時數：僅做判斷，不入庫
        try:
            service_hours = int(request.form.get('last_three_month_hours', 0))
        except ValueError:
            service_hours = 0

        # 原始金額（輸入）
        try:
            caregiver_raw = int(request.form.get('home_caregiver_insurance', 0))
        except ValueError:
            caregiver_raw = 0
        try:
            group_raw = int(request.form.get('group_accident_insurance', 0))
        except ValueError:
            group_raw = 0

        # ====== 保險補助（依規則折抵）======
        subsidy_rate = 0
        if one_year_passed and service_hours >= 131:
            subsidy_rate = 1
        elif one_year_passed and 101 <= service_hours <= 130:
            subsidy_rate = 0.5
        else:
            subsidy_rate = 0

        caregiver_final = caregiver_raw - int(caregiver_raw * subsidy_rate)
        group_final = group_raw - int(group_raw * subsidy_rate)

        values_dict['home_caregiver_insurance'] = caregiver_final
        values_dict['group_accident_insurance'] = group_final

        # ====== 健檢補助 ======
        enable_checkup = request.form.get('enable_checkup')
        if enable_checkup:
            if one_year_passed and service_hours >= 131:
                check_up_amount = 1200
            elif one_year_passed and 90 <= service_hours <= 130:
                check_up_amount = 600
            else:
                check_up_amount = 300
        else:
            check_up_amount = 0
        values_dict['check_up'] = check_up_amount

        # === 組裝資料 ===
        values = [values_dict.get(field, '0') if values_dict.get(field) is not None else None for field in all_fields]

        # === 更新或新增資料 ===
        cur.execute(
            "SELECT * FROM `employee_salary` WHERE `employee_id` = %s AND `year_month` = %s",
            (employee_id, year_month)
        )
        existing = cur.fetchone()

        changed = {}
        if existing:
            set_clause = ", ".join([f"`{field}` = %s" for field in all_fields])
            sql = f"UPDATE `employee_salary` SET {set_clause} WHERE `employee_id` = %s AND `year_month` = %s"
            cur.execute(sql, values + [employee_id, year_month])

            for idx, field in enumerate(all_fields):
                old_value = str(existing.get(field, ''))
                new_value = str(values[idx]) if values[idx] is not None else ''
                if old_value != new_value:
                    changed[field] = {"old": old_value, "new": new_value}

            if changed:
                log_edit(session['userid'], employee_id, 'edit_salary', changed)
        else:
            columns = ['employee_id', 'year_month'] + all_fields
            placeholders = ['%s'] * len(columns)
            sql = (
                f"INSERT INTO `employee_salary` ({', '.join(f'`{col}`' for col in columns)}) "
                f"VALUES ({', '.join(placeholders)})"
            )
            cur.execute(sql, [employee_id, year_month] + values)

            for idx, field in enumerate(all_fields):
                if values[idx] is not None and str(values[idx]) != '':
                    changed[field] = {"old": '', "new": str(values[idx])}

            if changed:
                log_edit(session['userid'], employee_id, 'add_salary', changed)

        mysql.connection.commit()
        session['salary_saved'] = True
        return redirect(url_for('employee_list'))

    # === GET 頁面載入 ===
    all_months = get_all_available_months(employee_id)

    cur.execute(
        "SELECT * FROM `employee_salary` WHERE `employee_id` = %s AND `year_month` = %s",
        (employee_id, selected_month)
    )
    salary = cur.fetchone()

    if not salary:
        salary = {'year_month': selected_month}
        for field in all_fields:
            salary[field] = 0
        salary['total_payment_insure_grade'] = info.get('labor_insurance_grade', 0)
        salary['health_insure_grade'] = info.get('health_insurance_grade', 0)
        
    else:
        for field in all_fields:
            if salary.get(field) is None:
                salary[field] = 0
        # 即使有紀錄，如果級距為 0，也可以考慮自動帶入預設（看客戶需求）
        if not salary.get('total_payment_insure_grade'):
            salary['total_payment_insure_grade'] = info.get('labor_insurance_grade', 0)
        if not salary.get('health_insure_grade'):
            salary['health_insure_grade'] = info.get('health_insurance_grade', 0)

    past_months = []
    # 往前推 1~2 個月 (range 1 到 3 代表產生 1, 2)
    for i in range(1, 3):
        past_dt = selected_dt - relativedelta(months=i)
        past_months.append(int(past_dt.strftime('%Y%m')))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        """SELECT daily_work_hr, holiday_work_hr 
           FROM employee_salary 
           WHERE employee_id = %s AND `year_month` IN (%s, %s)""",
        (employee_id, past_months[0], past_months[1])
    )
    past_records = cursor.fetchall()
    cursor.close()

    past_two_months_hours = 0
    for rec in past_records:
        daily = float(rec.get('daily_work_hr') or 0)
        holiday = float(rec.get('holiday_work_hr') or 0)
        past_two_months_hours += (daily + holiday)
    
    # === 特休天數計算（基於到職滿一年當月）===
    one_year_dt = datetime.strptime(str(one_year_month), "%Y%m")
    leave_days_calculated = calculate_leave_days(onboard_date, selected_dt)
    
    # 設定特休天數
    if is_past_one_year:
        # 如果目前資料庫沒有特休天數，或是為 0，就自動填入最新的計算結果
        if salary.get('leave_days') in [None, 0, '0']:
            salary['leave_days'] = leave_days_calculated
    else:
        # 未滿一年不給特休
        salary['leave_days'] = 0
    
    can_edit_paid_holiday = is_past_one_year

    # ✨✨ 判斷是否需要顯示特休發放提醒 ✨✨
    show_leave_reminder = False
    leave_payment_month_set = None
    
    if is_past_one_year:
        # 從 employee_info 查詢每年固定發放月份
        cursor = mysql.connection.cursor()
        cursor.execute(
            "SELECT leave_payment_month_of_year FROM employee_info WHERE employee_id = %s",
            (employee_id,)
        )
        info_record = cursor.fetchone()
        cursor.close()
        
        if info_record and info_record.get('leave_payment_month_of_year'):
            leave_payment_month_set = int(info_record['leave_payment_month_of_year'])
            
            # 提取當前月份 (從 202505 -> 5)
            current_month_num = int(str(selected_month)[4:6])
            
            # 比較月份
            if current_month_num == leave_payment_month_set:
                leave_deduction = salary.get('leave_deduction_amount', 0)
                try:
                    leave_deduction = float(leave_deduction)
                except (ValueError, TypeError):
                    leave_deduction = 0
                
                # 如果特休折抵金額為0，顯示提醒
                if leave_deduction == 0:
                    show_leave_reminder = True

    # === 生日禮金處理 ===
    salary['birthday_bonus'] = salary.get('birthday_bonus', 0) or calculate_birthday_bonus(
        info['employee_onboard'], info['employee_birth'], selected_month
    )

    # 供前端預覽健檢補助判斷
    today = datetime.today().date()
    one_year_passed = (today - info['employee_onboard']).days >= 365

    action = request.args.get('action', 'edit')
    
    # 取得員工補助資訊
    qualification = info.get('qualification', 0) if info else 0
    subsidy = info.get('subsidy', 0) if info else 0
    subsidy_none = info.get('subsidy_none', 0) if info else 0
    subsidy_half = info.get('subsidy_half', 0) if info else 0
    subsidy_full = info.get('subsidy_full', 0) if info else 0
    
    # ✨ 生成可選擇的月份列表（從滿一年開始到未來12個月）
    available_payment_months = []
    if is_past_one_year:
        current_year = datetime.today().year
        current_month_num = datetime.today().month
        
        # 從滿一年當月開始，生成24個月的選項
        start_dt = one_year_dt
        for i in range(24):
            month_dt = start_dt + relativedelta(months=i)
            month_value = int(month_dt.strftime('%Y%m'))
            month_display = month_dt.strftime('%Y年%m月')
            available_payment_months.append({
                'value': month_value,
                'display': month_display
            })
    
    return render_template('edit_salary_tabs.html',
                            employee_id=employee_id,
                            employee_name=employee_name,
                            salary=salary,
                            tabs=fields_by_tab,
                            all_months=all_months,
                            selected_month=selected_month,
                            data=salary,
                            action=action,
                            can_edit_paid_holiday=can_edit_paid_holiday,
                            paid_leave_days=leave_days_calculated,
                            one_year_passed=one_year_passed,
                            is_past_one_year=is_past_one_year,
                            one_year_month=one_year_month,
                            show_leave_reminder=show_leave_reminder,
                            leave_payment_month_set=leave_payment_month_set,
                            available_payment_months=available_payment_months,
                            qualification=qualification,
                            subsidy=subsidy,
                            subsidy_none=subsidy_none,
                            subsidy_half=subsidy_half,
                            subsidy_full=subsidy_full,
                            past_two_months_hours=past_two_months_hours,
                            info=info)  # ✨ 新增 info 傳遞

# ====== 預覽薪資資料 ======
@app.route('/preview_salary/<employee_id>')
@login_required
def preview_salary(employee_id):
    cursor = mysql.connection.cursor()
    # 🔥 新增：查詢員工姓名
    cursor.execute(
        "SELECT employee_name FROM employee_info WHERE employee_id = %s",
        (employee_id,)
    )
    name_result = cursor.fetchone()
    employee_name = name_result['employee_name'] if name_result else employee_id
    
    cursor.execute("SELECT * FROM `employee_salary` WHERE `employee_id` = %s ORDER BY `year_month` DESC", (employee_id,))
    salaries = cursor.fetchall()
    cursor.close()
    return render_template('preview_salary.html', employee_id=employee_id,employee_name=employee_name, salaries=salaries)

# ====== 新增薪資單 - 選擇月份頁面 ======
@app.route('/add_select_month/<employee_id>', methods=['GET', 'POST'])
@login_required
def add_select_month(employee_id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT employee_name, employee_onboard FROM employee_info WHERE employee_id = %s", (employee_id,))
    employee = cur.fetchone()
    if not employee:
        cur.close()
        flash("找不到該員工資料", "danger")
        return redirect(url_for('employee_list'))
    # ✨ 新增：取得該員工已經在資料庫中的所有薪資月份
    cur.execute("SELECT `year_month` FROM employee_salary WHERE employee_id = %s", (employee_id,))
    existing_records = cur.fetchall()
    cur.close()
    # 將已存在的月份存入 Set 中，方便後續快速比對 (例如: {'202601', '202602'})
    existing_months = set()
    for row in existing_records:
        # 兼容 dict 或 tuple 的回傳格式
        ym = row.get('year_month') if isinstance(row, dict) else row[0]
        if ym:
            existing_months.add(str(ym))
    
    # 1. 設定起始日 (Start Date) = 入職日
    onboard_date = employee.get('employee_onboard') if isinstance(employee, dict) else employee[1]
    if onboard_date:
        if isinstance(onboard_date, datetime):
            onboard_date = onboard_date.date()
        start_date = onboard_date.replace(day=1)
    else:
        # 如果沒入職日，預設從今年 1/1 開始
        start_date = date.today().replace(month=1, day=1)

    # 2. 設定結束日 (End Date) = 今天往後推一年
    today = date.today()
    target_date = today + relativedelta(years=1)
    end_date = target_date.replace(day=1)

    # 3. 生成結構化資料
    available_months_by_year = {}
    
    current_date = start_date
    while current_date <= end_date:
        y_str = str(current_date.year)
        m_int = current_date.month
        ym_str = f"{y_str}{m_int:02d}"
        
        # ✨ 關鍵過濾：如果該月份「不在」已存在的清單中，才加入到前端的下拉選單裡
        if ym_str not in existing_months:
            if y_str not in available_months_by_year:
                available_months_by_year[y_str] = []
            
            available_months_by_year[y_str].append(m_int)
        # 往後推一個月
        current_date += relativedelta(months=1)

    # 4. 產生年份列表 (從大到小排序，例如 [2027, 2026, 2025...])
    # 這裡轉成 int 是為了配合前端 select 迴圈
    years_list = sorted([int(y) for y in available_months_by_year.keys()], reverse=True)

    if request.method == 'POST':
        selected_year = request.form.get('year')
        selected_month = request.form.get('month')
        if selected_year and selected_month:
            year_month = f"{selected_year}{int(selected_month):02d}"
            # 後端雙重防呆驗證 (避免有人竄改前端 HTML 送出)
            if year_month in existing_months:
                flash(f"錯誤：{year_month} 的薪資資料已存在，請透過「編輯薪資」功能進行修改。", "danger")
                return redirect(request.url)
            # 重導向到編輯頁面
            return redirect(url_for('edit_salary_tabs', employee_id=employee_id, year_month=year_month, action='add'))

    return render_template('select_month.html',
                           title='選擇新增薪資月份',
                           button_text='確認並開始新增',
                           employee_id=employee_id,
                           employee_name=employee['employee_name'],
                           years=years_list,
                           available_months_by_year=available_months_by_year, # 這會傳遞動態生成的範圍
                           default_year=today.year,
                           default_month=today.month)

# ====== 編輯薪資單 - 選擇月份頁面 ======
@app.route('/edit_select_month/<employee_id>', methods=['GET', 'POST'])
@login_required
def edit_select_month(employee_id):
    cur = mysql.connection.cursor()
    cur.execute(
        "SELECT employee_name FROM employee_info WHERE employee_id = %s",
        (employee_id,)
    )
    name_result = cur.fetchone()
    employee_name = name_result['employee_name'] if name_result else employee_id
    cur.execute("SELECT DISTINCT `year_month` FROM `employee_salary` WHERE `employee_id` = %s ORDER BY `year_month` DESC", (employee_id,))
    months_data = [row['year_month'] for row in cur.fetchall()]
    cur.close()

    if not months_data:
        flash("該員工沒有薪資資料", "danger")
        return redirect(url_for('employee_list'))
    
    # 拆成年、月
    years = sorted({int(str(ym)[:4]) for ym in months_data}, reverse=True)
    available_months_by_year = {}
    for ym in months_data:
        year = int(str(ym)[:4])
        month = int(str(ym)[4:])
        available_months_by_year.setdefault(year, []).append(month)

    current_year = datetime.today().year
    current_month = datetime.today().month

    if request.method == 'POST':
        selected_year = request.form.get('year')
        selected_month = request.form.get('month')
        if selected_year and selected_month:
            year_month = f"{selected_year}{int(selected_month):02d}"
            return redirect(url_for('edit_salary_tabs', employee_id=employee_id, year_month=year_month, action='edit'))
        else:
            flash('請選擇年份與月份', 'danger')

    return render_template('select_month.html',
                           title='選擇編輯薪資月份',
                           button_text='確認並進入編輯頁面',
                           employee_id=employee_id,
                           employee_name=employee_name,
                           years=years,
                           available_months_by_year=available_months_by_year,
                           default_year=current_year,
                           default_month=current_month)

# ====== 匯出薪資單 - 選擇月份頁面 ======
@app.route('/export_pdf_select/<employee_id>', methods=['GET', 'POST'])
@login_required
def export_pdf_select(employee_id):
    cur = mysql.connection.cursor()
    cur.execute("SELECT DISTINCT `year_month` FROM `employee_salary` WHERE `employee_id` = %s ORDER BY `year_month` DESC", (employee_id,))
    months_data = [row['year_month'] for row in cur.fetchall()]
    cur.close()

    if not months_data:
        flash("該員工沒有薪資資料", "danger")
        return redirect(url_for('employee_list'))
    # 拆成年份與月份
    years = sorted({int(str(ym)[:4]) for ym in months_data}, reverse=True)
    available_months_by_year = {}
    for ym in months_data:
        year = int(str(ym)[:4])
        month = int(str(ym)[4:])
        available_months_by_year.setdefault(year, []).append(month)

    current_year = datetime.today().year
    current_month = datetime.today().month

    if request.method == 'POST':
        selected_year = request.form.get('year')
        selected_month = request.form.get('month')
        if selected_year and selected_month:
            year_month = f"{selected_year}{int(selected_month):02d}"
            return redirect(url_for('export_pdf', employee_id=employee_id, year_month=year_month))
        else:
            flash('請選擇年份與月份', 'danger')

    return render_template('export_pdf_select.html',
                            title='選擇匯出月份',
                            employee_id=employee_id,
                            years=years,
                            available_months_by_year=available_months_by_year,
                            default_year=current_year,
                            default_month=current_month,
                            button_text='匯出 PDF')

# ====== 匯出薪資單 - 生成 PDF ======
@app.route('/export_pdf/<employee_id>/<year_month>')
@login_required
def export_pdf(employee_id, year_month):
    from urllib.parse import quote
    
    cur = mysql.connection.cursor()
    
    # 查詢員工姓名
    cur.execute("SELECT employee_name FROM employee_info WHERE employee_id = %s", (employee_id,))
    employee_info = cur.fetchone()
    employee_name = employee_info['employee_name'] if employee_info else employee_id
    
    # 查詢薪資資料
    cur.execute(
        "SELECT * FROM `view_salary_export` WHERE `員工編號` = %s AND `年/月` = %s",
        (employee_id, year_month)
    )
    rows = cur.fetchall()
    cur.close()

    if not rows:
        return "無資料可匯出", 404

    rendered = render_template('salary_pdf_template.html', rows=rows, employee_id=employee_id)

    configuration = pdfkit.configuration(wkhtmltopdf=r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe')
    pdf_file = pdfkit.from_string(rendered, False, configuration=configuration)

    response = make_response(pdf_file)
    response.headers['Content-Type'] = 'application/pdf'
    # 修改檔案名稱為：員工姓名+年月.pdf，使用 URL 編碼處理中文
    filename = f"{employee_name}{year_month}.pdf"
    encoded_filename = quote(filename)
    response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
    return response

# ====== 匯出個人資料庫資料 ======
@app.route('/export_excel/<employee_id>')
@login_required
def export_excel(employee_id):
    cur = mysql.connection.cursor()

    # ✅ 查詢該員工所有資料
    query = """
        SELECT 
            s.`year_month`, s.`employee_id`, i.`employee_birth`, i.`employee_card`, i.`employee_name`, i.`employee_onboard`,
            s.`capacity_amout`, s.`Split_amount`, s.`AA09_bonus_view`, s.`daily_work_hr`, s.`holiday_work_hr`, s.`total_hr`,
            s.`daily_work_salary`, s.`duty_allowance`, s.`transition_hr`, s.`transition_allowance`,s.`Overtime_pay_weekdays910hr`, 
            s.`Overtime_pay_weekdays1112hr`, s.`total_overtime_pay_weekdays`,s.`Overtime_pay_rest_days12hr`, 
            s.`Overtime_pay_rest_days38hr`, s.`total_overtime_pay_restdays`,s.`holiday_overtime_hr`, s.`total_pay_holidays`, 
            s.`National_holidays_hr`, s.`total_pay_national_holidays`,s.`main_total__overtime_pay`,s.`Performance_bonuses`, 
            s.`AA09_bonus`, s.`Salary_subtotal`, s.`Inter_district_subsidie`,s.`leave_pay_days`, s.`leave_deduction_amount`, 
            s.`ultimate_salary`, s.`BGA_view`,s.`licence_allowance`, s.`holiday_bonus`, s.`birthday_bonus`,s.`referral_fee`, 
            s.`travel_allowance`, s.`check_up`, s.`training_fee`, s.`teaching_subsidy`,s.`new_case_allowance`, s.`extras`, s.`extras_amout`,s.`extras2`,s.`extras2_amount`,
            s.`total_welfare`,s.`total_payment_insure_grade`, s.`Labor_premiums`, s.`Insurance_amount`, s.`retirement_plan_withdraw`,
            s.`home_caregiver_insurance`, s.`group_accident_insurance`, s.`subtotal_withholding`,s.`additional_withholding_items`,s.`additional_withholding_amout`,s.`additional_withholding2_items`,
            s.`additional_withholding2_amount`,s.`payroll_due`,s.`paid_in_salary`, s.`tenth_salary`, s.`thirtieth_salary`, s.`remarks`
        FROM employee_salary s
        JOIN employee_info i ON s.employee_id = i.employee_id
        WHERE s.employee_id = %s
    """
    cur.execute(query, (employee_id,))
    rows = cur.fetchall()
    
    if not rows:
        return f"找不到 employee_id 為 {employee_id} 的資料", 404

    # 取得員工姓名（從第一筆資料中取得）
    employee_name = rows[0]['employee_name'] if rows else employee_id

    # ✅ 轉換為 pandas DataFrame
    df = pd.DataFrame(rows)

    # ✅ 將英文欄位名稱轉成中文
    df.rename(columns=column_mapping, inplace=True)

    # ✅ 寫入 Excel 檔案
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='員工薪資資料')

        workbook  = writer.book
        worksheet = writer.sheets['員工薪資資料']

        # 置中
        center_format = workbook.add_format({'align': 'center', 'valign': 'vcenter'})

        # 根據欄名 & 資料內容動態調整欄寬
        for i, col in enumerate(df.columns):
            # 計算最大字數 (標題 + 該欄所有資料)
            max_length = max(
                df[col].astype(str).map(len).max(),  # 資料最大長度
                len(col)  # 標題長度
            )
            # 中文通常比英文寬 → 給一個係數 (1.8~2 比較自然)
            worksheet.set_column(i, i, max_length * 2, center_format)

        # 套用整個表格置中 (標題 + 資料列)
        worksheet.set_row(0, None, center_format)   # 標題列
        for row_num in range(1, len(df) + 1):
            worksheet.set_row(row_num, None, center_format)
    output.seek(0)

    # ✅ 傳回 Excel 檔案，修改檔案名稱為：員工姓名薪資總表.xlsx
    filename = f"{employee_name}薪資總表.xlsx"
    return send_file(output,
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    as_attachment=True,
                    download_name=filename)

# ====== 匯出 Excel - 選擇月份頁面 ======
@app.route('/export_excel_select', methods=['GET', 'POST'])
@login_required
def export_excel_select():
    cur = mysql.connection.cursor()
    # 取出所有有資料的年月 (例如 202401, 202402...)
    cur.execute("SELECT DISTINCT `year_month` FROM `employee_salary` ORDER BY `year_month` DESC")
    months_data = [row['year_month'] for row in cur.fetchall()]
    cur.close()

    if not months_data:
        flash("目前沒有任何薪資資料可供匯出", "danger")
        return redirect(url_for('salary_setting'))

    # 拆成年份與月份，整理成 dict 給前端用
    years = sorted({int(str(ym)[:4]) for ym in months_data}, reverse=True)
    available_months_by_year = {}
    for ym in months_data:
        year = int(str(ym)[:4])
        month = int(str(ym)[4:])
        available_months_by_year.setdefault(year, []).append(month)

    current_year = datetime.today().year
    current_month = datetime.today().month

    if request.method == 'POST':
        selected_year = request.form.get('year')
        selected_month = request.form.get('month')
        if selected_year and selected_month:
            year_month = f"{selected_year}{int(selected_month):02d}"
            return redirect(url_for('export_excel_by_month', year_month=year_month))
        else:
            flash('請選擇年份與月份', 'danger')

    return render_template('export_excel_select.html',
                           title='選擇匯出月份',
                           years=years,
                           available_months_by_year=available_months_by_year,
                           default_year=current_year,
                           default_month=current_month,
                           button_text='匯出 Excel')

# ====== 匯出 Excel - 生成 Excel ======
@app.route('/export_excel_by_month/<year_month>')
@login_required
def export_excel_by_month(year_month):
    cur = mysql.connection.cursor()

    # ✅ 查詢該月份所有員工資料
    query = """
        SELECT 
            s.`year_month`, s.`employee_id`, i.`employee_birth`, i.`employee_card`, i.`employee_name`, i.`employee_onboard`,
            s.`capacity_amout`, s.`Split_amount`, s.`AA09_bonus_view`, s.`daily_work_hr`, s.`holiday_work_hr`, s.`total_hr`,
            s.`daily_work_salary`, s.`duty_allowance`, s.`transition_hr`, s.`transition_allowance`,s.`Overtime_pay_weekdays910hr`, 
            s.`Overtime_pay_weekdays1112hr`, s.`total_overtime_pay_weekdays`,s.`Overtime_pay_rest_days12hr`, 
            s.`Overtime_pay_rest_days38hr`, s.`total_overtime_pay_restdays`,s.`holiday_overtime_hr`, s.`total_pay_holidays`, 
            s.`National_holidays_hr`, s.`total_pay_national_holidays`,s.`main_total__overtime_pay`,s.`Performance_bonuses`, 
            s.`AA09_bonus`, s.`Salary_subtotal`, s.`Inter_district_subsidie`,s.`leave_pay_days`, s.`leave_deduction_amount`, 
            s.`ultimate_salary`, s.`BGA_view`,s.`licence_allowance`, s.`holiday_bonus`, s.`birthday_bonus`,s.`referral_fee`, 
            s.`travel_allowance`, s.`check_up`, s.`training_fee`, s.`teaching_subsidy`,s.`new_case_allowance`, s.`extras`, s.`extras_amout`,s.`extras2`,s.`extras2_amount`,
            s.`total_welfare`,s.`total_payment_insure_grade`, s.`Labor_premiums`, s.`Insurance_amount`, s.`retirement_plan_withdraw`,
            s.`home_caregiver_insurance`, s.`group_accident_insurance`, s.`subtotal_withholding`,s.`additional_withholding_items`,s.`additional_withholding_amout`,s.`additional_withholding2_items`,
            s.`additional_withholding2_amount`,s.`payroll_due`,s.`paid_in_salary`, s.`tenth_salary`, s.`thirtieth_salary`, s.`remarks`
        FROM employee_salary s
        JOIN employee_info i ON s.employee_id = i.employee_id
        WHERE s.year_month = %s
    """
    cur.execute(query, (year_month,))
    rows = cur.fetchall()
    
    if not rows:
        return f"找不到 {year_month} 的薪資資料", 404

    # ✅ 轉換為 pandas DataFrame
    df = pd.DataFrame(rows, columns=[desc[0] for desc in cur.description])
    cur.close()

    # ✅ 將英文欄位名稱轉成中文（假設你有 column_mapping）
    df.rename(columns=column_mapping, inplace=True)

    # ✅ 寫入 Excel 檔案
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='員工薪資資料')

        workbook  = writer.book
        worksheet = writer.sheets['員工薪資資料']
        center_format = workbook.add_format({'align': 'center', 'valign': 'vcenter'})

        # ✅ 根據欄名 & 資料內容動態調整欄寬 (修正版)
        for i, col in enumerate(df.columns):
            # 1. 取得該欄位所有資料，轉為字串並計算長度
            # 使用 str.len() 是 pandas 內建處理字串長度最安全的方法
            column_len = df[col].astype(str).str.len().max()
            
            # 2. 取得標題長度
            header_len = len(str(col))
            
            # 3. 取兩者最大值
            max_length = max(column_len, header_len)
            
            # 4. 設定欄寬 (考慮到中文，係數 2 是合理的；若全是數字可縮小至 1.2)
            worksheet.set_column(i, i, max_length * 2, center_format)

        worksheet.set_row(0, None, center_format)
        for row_num in range(1, len(df) + 1):
            worksheet.set_row(row_num, None, center_format)

    output.seek(0)
    filename = f"{year_month}薪資總表.xlsx"

    return send_file(output,
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    as_attachment=True,
                    download_name=filename)

# ====== 刪除功能 ======
@app.route('/hide_employee/<employee_id>', methods=['POST'])
@login_required
def hide_employee(employee_id):
    cur = mysql.connection.cursor()
    cur.execute("UPDATE employee_info SET is_hidden = TRUE WHERE employee_id = %s", (employee_id,))
    mysql.connection.commit()
    cur.close()
    flash("員工資料已刪除", "info")
    return redirect(url_for('employee_manage'))

# ====== 勞保級距設定 ======
@app.route('/get_labor_insurance_levels/<int:year>')
@login_required
def get_labor_insurance_levels(year):
    cursor = mysql.connection.cursor()
    # 優先抓取指定年份，如果該年沒資料，則抓取最新的年份
    cursor.execute("SELECT * FROM labor_insurance_level WHERE effective_year = %s ORDER BY `range` ASC", (year,))
    levels = cursor.fetchall()
    
    if not levels: # 防呆：如果找不到該年份，抓最近的一年
        cursor.execute("SELECT * FROM labor_insurance_level WHERE effective_year = (SELECT MAX(effective_year) FROM labor_insurance_level) ORDER BY `range` ASC")
        levels = cursor.fetchall()
        
    cursor.close()
    return jsonify(levels)

# ====== 下載勞保級距CSV範本 ======
@app.route('/download_labor_insurance_template')
@login_required
def download_labor_insurance_template():
    # 建立範本數據
    template_data = {'range': ['28800', '32400', '34500'],
                    'employee_labor': [644, 686, 730],
                    'company_labor': [2148, 2287, 2433],
                    'company_occu': [86, 92, 97],
                    'company_retire': [1728, 1944, 2070],
                    'company_total': [4807, 5227, 5562]}
    
    # 創建DataFrame
    df = pd.DataFrame(template_data)
    
    # 寫入CSV
    output = io.StringIO()
    df.to_csv(output, index=False, encoding='utf-8-sig')
    output.seek(0)
    
    return send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')),
                    mimetype='text/csv',
                    as_attachment=True,
                    download_name='labor_insurance_template.csv')

# ====== 勞保級距管理路由 ======
@app.route('/labor_insurance_level_manage', methods=['GET', 'POST'])
@login_required
def labor_insurance_level_manage():
    cursor = mysql.connection.cursor()
    
    # 1. 取得所有年份供標籤顯示
    cursor.execute("SELECT DISTINCT effective_year FROM labor_insurance_level ORDER BY effective_year DESC")
    available_years = [row['effective_year'] for row in cursor.fetchall()]
    
    # 2. 決定目前顯示年份
    selected_year = request.args.get('year', type=int)
    if not selected_year and available_years:
        selected_year = available_years[0]
    elif not selected_year:
        selected_year = datetime.now().year

    if request.method == 'POST':
        if 'file' in request.files:
            file = request.files['file']
            # 前端標籤的年份（作為備用）
            fallback_year = request.form.get('year', selected_year, type=int)
            
            try:
                df = pd.read_csv(io.BytesIO(file.read()), encoding='utf-8-sig')
                df.columns = [c.lower().strip() for c in df.columns]

                def clean_val(value):
                    if pd.isna(value) or str(value).strip() == '': return 0
                    return int(str(value).replace(',', '').replace(' ', '').split('.')[0])

                # ✨ 核心修正：偵測 CSV 內是否有年份欄位
                has_year_col = 'effective_year' in df.columns
                
                # 找出 CSV 中所有的年份並執行精準刪除 (避免重複)
                if has_year_col:
                    import_years = df['effective_year'].unique()
                    for y in import_years:
                        cursor.execute("DELETE FROM labor_insurance_level WHERE effective_year = %s", (int(y),))
                else:
                    cursor.execute("DELETE FROM labor_insurance_level WHERE effective_year = %s", (fallback_year,))
                
                # 逐行寫入
                for _, row in df.iterrows():
                    # ✨ 優先使用 CSV 裡的年份
                    row_year = int(row['effective_year']) if has_year_col else fallback_year
                    
                    cursor.execute("""
                        INSERT INTO labor_insurance_level 
                        (`range`, `employee_labor`, `company_labor`, `company_occu`, `company_retire`, `effective_year`)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        clean_val(row['range']), 
                        clean_val(row['employee_labor']), 
                        clean_val(row['company_labor']), 
                        clean_val(row['company_occu']), 
                        clean_val(row['company_retire']), 
                        row_year
                    ))
                
                mysql.connection.commit()
                return jsonify({'success': True})
            except Exception as e:
                mysql.connection.rollback()
                return jsonify({'success': False, 'error': str(e)})

        return redirect(url_for('labor_insurance_level_manage', year=selected_year))

    cursor.execute("SELECT * FROM labor_insurance_level WHERE effective_year = %s ORDER BY `range` ASC", (selected_year,))
    levels = cursor.fetchall()
    cursor.close()
    return render_template('labor_insurance_level_manage.html', levels=levels, available_years=available_years, selected_year=selected_year)
    
# ====== 健保級距設定 ======
@app.route('/get_health_insurance_levels/<int:year>')
@login_required
def get_health_insurance_levels(year):
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM health_insurance_level WHERE effective_year = %s ORDER BY `Range` ASC", (year,))
    levels = cursor.fetchall()
    
    if not levels: # 防呆
        cursor.execute("SELECT * FROM health_insurance_level WHERE effective_year = (SELECT MAX(effective_year) FROM health_insurance_level) ORDER BY `Range` ASC")
        levels = cursor.fetchall()
        
    cursor.close()
    return jsonify(levels)

# ====== 下載勞保級距CSV範本 ======
@app.route('/download_health_insurance_template')
@login_required
def download_health_insurance_template():
    # 建立範本數據
    template_data = {
        'Range': ['28800', '32400', '34500'],
        'employee_and0': [644, 686, 730],
        'employee_and1': [423, 452, 481],
        'employee_and2': [2148, 2287, 2433],
        'employee_and3': [86, 92, 97],
        'company_health': [845, 904, 962],}
    
    # 創建DataFrame
    df = pd.DataFrame(template_data)
    
    # 寫入CSV
    output = io.StringIO()
    df.to_csv(output, index=False, encoding='utf-8-sig')
    output.seek(0)
    
    return send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')),
                    mimetype='text/csv',
                    as_attachment=True,
                    download_name='health_insurance_template.csv')
    

# ====== 健保級距管理路由 ======
@app.route('/health_insurance_level_manage', methods=['GET', 'POST'])
@login_required
def health_insurance_level_manage():
    cursor = mysql.connection.cursor()
    
    # 1. 取得所有年份 (供分頁標籤顯示)
    cursor.execute("SELECT DISTINCT effective_year FROM health_insurance_level ORDER BY effective_year DESC")
    available_years = [row['effective_year'] for row in cursor.fetchall()]
    
    # 2. 決定目前查看的年份
    selected_year = request.args.get('year', type=int)
    if not selected_year and available_years:
        selected_year = available_years[0]
    elif not selected_year:
        selected_year = datetime.now().year

    if request.method == 'POST':
        # ✨ 處理 AJAX CSV 批量匯入
        if 'file' in request.files:
            file = request.files['file']
            # 前端標籤選定的年份 (備用)
            fallback_year = request.form.get('year', selected_year, type=int)
            
            try:
                # 讀取 CSV
                df = pd.read_csv(io.BytesIO(file.read()), encoding='utf-8-sig')
                
                # 統一標題為小寫並去除兩端空白，並相容 Range/range
                df.rename(columns={'Range': 'range'}, inplace=True)
                df.columns = [c.lower().strip() for c in df.columns]

                # 定義資料清洗函數：解決 "data truncated" (移除逗號、空格)
                def clean_val(value):
                    if pd.isna(value) or str(value).strip() == '':
                        return 0
                    # 移除逗號、空白、小數點以後的內容，確保轉為純整數
                    cleaned = str(value).replace(',', '').replace(' ', '').replace('$', '').split('.')[0]
                    return int(cleaned)

                # ✨ 核心修正：偵測 CSV 內是否有 effective_year 欄位
                has_year_col = 'effective_year' in df.columns
                
                # 找出 CSV 中包含的所有年份，執行精準刪除，防止不同年份資料混雜
                if has_year_col:
                    import_years = df['effective_year'].unique().tolist()
                    for y in import_years:
                        cursor.execute("DELETE FROM health_insurance_level WHERE effective_year = %s", (int(y),))
                else:
                    cursor.execute("DELETE FROM health_insurance_level WHERE effective_year = %s", (fallback_year,))
                    import_years = [fallback_year]
                
                # 逐行清洗並匯入
                for _, row in df.iterrows():
                    # 優先使用 CSV 內的年份，若無則使用標籤年份
                    row_year = int(row['effective_year']) if has_year_col else fallback_year
                    
                    cursor.execute("""
                        INSERT INTO health_insurance_level 
                        (`Range`, `employee_and0`, `employee_and1`, `employee_and2`, `employee_and3`, `company_health`, `effective_year`)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        clean_val(row['range']), 
                        clean_val(row['employee_and0']),
                        clean_val(row['employee_and1']), 
                        clean_val(row['employee_and2']),
                        clean_val(row['employee_and3']), 
                        clean_val(row['company_health']),
                        row_year
                    ))
                
                mysql.connection.commit()
                return jsonify({'success': True, 'years': import_years})
            except Exception as e:
                mysql.connection.rollback()
                print(f"DEBUG - 健保匯入報錯: {str(e)}") # 這會印在您的 Python 終端機
                return jsonify({'success': False, 'error': str(e)})

        # 保留單筆修改/刪除邏輯...
        action = request.form.get('action')
        # ... 原本邏輯 ...
        
        return redirect(url_for('health_insurance_level_manage', year=selected_year))

    # GET 請求：撈取指定年份
    cursor.execute("SELECT * FROM health_insurance_level WHERE effective_year = %s ORDER BY `Range` ASC", (selected_year,))
    levels = cursor.fetchall()
    cursor.close()
    
    return render_template('health_insurance_level_manage.html', 
                            levels=levels, 
                            available_years=available_years, 
                            selected_year=selected_year)
# ==========薪資異動紀錄路由 (只看薪資相關)============
@app.route('/edit_log_salary/<employee_id>')
@login_required
def edit_log_salary(employee_id):
    cursor = mysql.connection.cursor()
    
    # 抓取員工基本資料 (顯示在標題用)
    cursor.execute("SELECT employee_name FROM employee_info WHERE employee_id = %s", (employee_id,))
    emp = cursor.fetchone()
    employee_name = emp['employee_name'] if emp else "未知員工"

    # 抓取 Log，但只篩選「薪資相關」的動作
    # 假設你的 action_type 是 'edit_salary', 'add_salary'
    cursor.execute("""
        SELECT * FROM edit_log 
        WHERE employee_id = %s 
        AND action_type IN ('edit_salary', 'add_salary')
        ORDER BY timestamp DESC
    """, (employee_id,))
    logs = cursor.fetchall()
    
    # 將變更欄位 JSON 字串轉換為字典
    for log in logs:
        # 轉換變更欄位為 dict
        original_fields = json.loads(log['changed_fields'])

        # 將欄位名稱轉換為中文
        translated_fields = {}
        for key, value in original_fields.items():
            chinese_key = column_mapping.get(key, key)
            translated_fields[chinese_key] = value

        log['changed_fields'] = translated_fields
        log['action_type'] = action_mapping.get(log['action_type'], log['action_type'])
    

    cursor.close()
    return render_template('edit_log_salary.html', logs=logs, employee_id=employee_id, employee_name=employee_name)

# =============個人資料異動紀錄路由 (排除薪資相關)================
@app.route('/edit_log_profile/<employee_id>')
@login_required
def edit_log_profile(employee_id):
    cursor = mysql.connection.cursor()
    
    cursor.execute("SELECT employee_name FROM employee_info WHERE employee_id = %s", (employee_id,))
    emp = cursor.fetchone()
    employee_name = emp['employee_name'] if emp else "未知員工"

    # 抓取 Log，篩選「非薪資」的動作 (個資修改、離職、復職)
    # 假設 action_type 包含 'edit_employee', 'resign', 'reinstate'
    cursor.execute("""
        SELECT * FROM edit_log 
        WHERE employee_id = %s 
        AND action_type NOT IN ('edit_salary', 'add_salary')
        ORDER BY timestamp DESC
    """, (employee_id,))
    logs = cursor.fetchall()
    
    # 將變更欄位 JSON 字串轉換為字典
    for log in logs:
        # 轉換變更欄位為 dict
        original_fields = json.loads(log['changed_fields'])

        # 將欄位名稱轉換為中文
        translated_fields = {}
        for key, value in original_fields.items():
            chinese_key = column_mapping.get(key, key)
            translated_fields[chinese_key] = value

        log['changed_fields'] = translated_fields
        log['action_type'] = action_mapping.get(log['action_type'], log['action_type'])

    cursor.close()
    return render_template('edit_log_profile.html', logs=logs, employee_id=employee_id, employee_name=employee_name)

# 1. 顯示匯入頁面 (提供範本下載連結)
@app.route('/import_history_select')
@login_required
def import_history_select():
    return render_template('import_history.html')

# 2. 處理 Excel 匯入 (包含詳細錯誤編號顯示)
@app.route('/import_history_process', methods=['POST'])
@login_required
def import_history_process():
    if 'file' not in request.files:
        flash('未選擇檔案', 'danger')
        return redirect(request.url)
    
    file = request.files['file']
    if file.filename == '':
        flash('檔案名稱為空', 'danger')
        return redirect(request.url)

    try:
        # 1. 讀取 Excel 並處理標題對應
        df = pd.read_excel(file)
        reverse_mapping = {v: k for k, v in column_mapping.items()}
        df.rename(columns=reverse_mapping, inplace=True)

        cur = mysql.connection.cursor()
        
        # 獲取資料庫欄位清單
        cur.execute("DESCRIBE `employee_salary`")
        desc_result = cur.fetchall()
        db_columns = [row['Field'] if isinstance(row, dict) else row[0] for row in desc_result]
        
        success_count = 0
        skipped_ids = []

        for index, row in df.iterrows():
            employee_id = str(row.get('employee_id', '')).strip()
            year_month_raw = str(row.get('year_month', '')).replace('.0', '')
            
            if not employee_id or employee_id == 'nan' or not year_month_raw:
                continue

            # 2. 獲取該員工的基本身分資訊 (用於保險計算)
            cur.execute("""
                SELECT qualification, subsidy FROM employee_info 
                WHERE employee_id = %s
            """, (employee_id,))
            emp_info = cur.fetchone()
            
            if not emp_info:
                skipped_ids.append(employee_id)
                continue
            
            # 解析年份 (例如 202405 -> 2024)
            year = int(year_month_raw[:4])

            # 3. 準備基礎數據與處理空值
            import_data = {}
            for k, v in row.to_dict().items():
                if k in db_columns:
                    if pd.isnull(v):
                        import_data[k] = 0 if any(x in k.lower() for x in ['hr', 'bonus', 'amout', 'amount', 'salary', 'grade']) else None
                    else:
                        import_data[k] = v
            
            # 4. 🌟 核心補值邏輯：自動查表填入「公司負擔」相關欄位
            
            # (A) 勞保、職保、勞退 6% 補值
            labor_grade = safe_int(import_data.get('total_payment_insure_grade', 0))
            if labor_grade > 0:
                labor_res = get_full_labor_details(cur, labor_grade, year)
                if labor_res:
                    # 如果 Excel 中的自付額是 0，則從級距表補回
                    if safe_int(import_data.get('Labor_premiums', 0)) == 0:
                        import_data['Labor_premiums'] = labor_res.get('employee_labor', 0)
                    
                    # 強制填入公司負擔額
                    import_data['company_labor'] = labor_res.get('company_labor', 0)
                    import_data['company_occu'] = labor_res.get('company_occu', 0)
                    import_data['company_retire'] = labor_res.get('company_retire', 0)

            # (B) 健保公出補值
            health_grade = safe_int(import_data.get('health_insure_grade', 0))
            if health_grade > 0:
                health_res = get_full_health_details(
                    cur, health_grade, emp_info['qualification'], emp_info['subsidy'], year
                )
                if health_res:
                    # 如果 Excel 中的自付額是 0，補回算好的自付額
                    if safe_int(import_data.get('Insurance_amount', 0)) == 0:
                        import_data['Insurance_amount'] = health_res.get('Insurance_amount', 0)
                    
                    # 強制填入單位健保負擔
                    import_data['company_health'] = health_res.get('company_health', 0)

            # 5. 執行寫入 (包含重複月份更新)
            if not import_data: continue

            fields = list(import_data.keys())
            placeholders = ', '.join(['%s'] * len(fields))
            columns_str = ', '.join([f'`{f}`' for f in fields])
            update_clause = ', '.join([f'`{f}` = VALUES(`{f}`)' for f in fields])
            
            sql = f"""
                INSERT INTO `employee_salary` ({columns_str}) 
                VALUES ({placeholders}) 
                ON DUPLICATE KEY UPDATE {update_clause}
            """
            
            cur.execute(sql, tuple(import_data.values()))
            success_count += 1
        
        mysql.connection.commit()
        cur.close()
        
        flash(f'✅ 成功匯入/更新 {success_count} 筆資料。公司負擔額已根據級距表自動補齊。', 'success')
        if skipped_ids:
            flash(f'⚠️ 提示：員工編號 {list(set(skipped_ids))} 不存在，已跳過。', 'warning')
        
    except Exception as e:
        if 'cur' in locals(): mysql.connection.rollback()
        flash(f'❌ 匯入失敗：{str(e)}', 'danger')

    return redirect(url_for('user'))

def get_full_labor_details(cursor, grade_val, year):
    """根據年份與勞保級距，抓取員工自付與單位負擔明細"""
    try:
        # 尋找該年份最接近且大於等於該金額的級距
        cursor.execute("""
            SELECT `employee_labor`, `company_labor`, `company_occu`, `company_retire`
            FROM labor_insurance_level 
            WHERE `range` >= %s AND effective_year = %s 
            ORDER BY `range` ASC LIMIT 1
        """, (grade_val, year))
        res = cursor.fetchone()
        
        # 防呆：若該年無資料則抓最新年份
        if not res:
            cursor.execute("""
                SELECT `employee_labor`, `company_labor`, `company_occu`, `company_retire`
                FROM labor_insurance_level 
                WHERE `range` >= %s ORDER BY effective_year DESC, `range` ASC LIMIT 1
            """, (grade_val,))
            res = cursor.fetchone()
        return res or {}
    except Exception as e:
        print(f"勞保查表失敗: {e}")
        return {}

def get_full_health_details(cursor, grade_val, dependents, subsidy_type, year):
    """根據年份與健保級距，計算員工自付額並抓取單位負擔"""
    try:
        cursor.execute("""
            SELECT `employee_and0`, `employee_and1`, `employee_and2`, `employee_and3`, `company_health`
            FROM health_insurance_level 
            WHERE `Range` >= %s AND effective_year = %s 
            ORDER BY `Range` ASC LIMIT 1
        """, (grade_val, year))
        res = cursor.fetchone()
        
        if not res:
            cursor.execute("""
                SELECT `employee_and0`, `employee_and1`, `employee_and2`, `employee_and3`, `company_health`
                FROM health_insurance_level 
                WHERE `Range` >= %s ORDER BY effective_year DESC, `Range` ASC LIMIT 1
            """, (grade_val,))
            res = cursor.fetchone()
            
        if not res: return {}

        # 計算員工自付額 (考慮補助)
        q = min(int(dependents or 0), 3)
        base_field = f'employee_and{q}'
        # 相容大小寫
        self_pay = res.get(base_field) or res.get(base_field.capitalize()) or 0
        
        s = int(subsidy_type or 0)
        final_self_pay = 0
        if s == 2: final_self_pay = int(float(self_pay) * 0.5)
        elif s != 5: final_self_pay = int(self_pay)
        
        return {
            'Insurance_amount': final_self_pay,
            'company_health': res.get('company_health', 0)
        }
    except Exception as e:
        print(f"健保查表失敗: {e}")
        return {}
# ====== 員工保險級距異動歷史查詢 ======
@app.route('/insurance_history/<employee_id>')
@login_required
def insurance_history(employee_id):
    cursor = mysql.connection.cursor()
    
    # 1. 抓取員工基本資訊
    cursor.execute("SELECT employee_name FROM employee_info WHERE employee_id = %s", (employee_id,))
    emp = cursor.fetchone()
    employee_name = emp['employee_name'] if emp else "未知員工"

    # 2. 抓取所有個人資料修改紀錄 (篩選 edit_employee)
    cursor.execute("""
        SELECT timestamp, changed_fields, userid 
        FROM edit_log 
        WHERE employee_id = %s AND action_type = 'edit_employee'
        ORDER BY timestamp DESC
    """, (employee_id,))
    logs = cursor.fetchall()

    history_data = []
    for log in logs:
        # 解析 JSON 格式的變更欄位
        try:
            changes = json.loads(log['changed_fields'])
        except:
            continue
            
        # ✨ 篩選出包含「級距」字眼的變動
        insurance_changes = {}
        for field, val in changes.items():
            # 將英文欄位名對應到中文，並判斷是否為級距相關
            chinese_field = column_mapping.get(field, field)
            if '級距' in chinese_field:
                insurance_changes[chinese_field] = val
        
        # 如果這筆紀錄包含級距變動，則加入歷史清單
        if insurance_changes:
            history_data.append({
                'time': log['timestamp'],
                'user': log['userid'],
                'details': insurance_changes
            })

    cursor.close()
    return render_template('insurance_history.html', 
                           employee_id=employee_id, 
                           employee_name=employee_name, 
                           history=history_data)
if __name__ == '__main__':
    app.run(debug=True)
