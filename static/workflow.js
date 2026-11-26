/* Shared notices, unsaved form protection, import previews and downloads. */
(() => {
    'use strict';
    let navigating = false;
    const forms = new Map();
    const headers = { 'X-Workflow-Request': '1' };

    function notice(message, category = 'info', key = 'workflow', target) {
        const container = target || document.getElementById('workflow-notices');
        if (!container) return;
        let box = document.getElementById(`notice-${key}`);
        if (!message) { box?.remove(); return; }
        if (!box) {
            box = document.createElement('div');
            box.id = `notice-${key}`;
            container.appendChild(box);
        }
        box.className = `flash-message ${category}`;
        box.setAttribute('role', category === 'danger' ? 'alert' : 'status');
        box.textContent = message;
        return box;
    }

    function reportInsuranceLevels(label, levels, year, savedGrade) {
        const target = document.getElementById('insurance-year-notices');
        const key = label === '勞保' ? 'labor-year' : 'health-year';
        if (!Array.isArray(levels) || !levels.length) {
            notice(`${label}級距尚無可用資料，請先補齊級距表並核對金額。`, 'danger', key, target);
            return;
        }
        const actualYear = levels[0].effective_year;
        const messages = [];
        if (actualYear && String(actualYear) !== String(year)) {
            messages.push(`找不到 ${year} 年度${label}級距，目前使用 ${actualYear} 年度資料，請確認。`);
        }
        if (Number(savedGrade) > 0 && !levels.some(level => String(level.range || level.Range) === String(savedGrade))) {
            messages.push(`原本的${label}級距 ${savedGrade} 不在目前級距表中；請核對級距與已儲存金額。`);
        }
        notice(messages.join('\n'), 'warning', key, target);
    }

    function snapshot(form) {
        return JSON.stringify(Array.from(new FormData(form)).map(([key, value]) =>
            [key, value instanceof File ? `${value.name}:${value.size}` : value]));
    }

    function dirty() {
        return Array.from(forms).some(([form, state]) => state.touched && snapshot(form) !== state.baseline);
    }

    function busy(button, text) {
        if (!button) return () => {};
        const original = button.innerHTML;
        button.disabled = true;
        button.textContent = text;
        return () => { button.disabled = false; button.innerHTML = original; };
    }

    async function jsonResponse(response) {
        if (!(response.headers.get('Content-Type') || '').includes('application/json')) {
            throw new Error('系統回應異常，請確認連線或重新登入。目前輸入仍保留在此頁。');
        }
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.error || '操作未完成，請重試。');
        return result;
    }

    function errorNotice(error) {
        const message = /Failed to fetch|NetworkError|Load failed/i.test(error.message || '')
            ? '連線中斷，請先確認處理結果後再重試；目前輸入仍保留在此頁。'
            : (error.message || '連線中斷，請確認處理結果後再重試。');
        const box = notice(message, 'danger');
        box?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    async function download(url, options = {}) {
        const response = await fetch(url, { ...options, headers });
        const disposition = response.headers.get('Content-Disposition') || '';
        if (!response.ok || !disposition.includes('attachment')) {
            if ((response.headers.get('Content-Type') || '').includes('application/json')) {
                await jsonResponse(response);
            }
            const html = new DOMParser().parseFromString(await response.text(), 'text/html');
            const messages = Array.from(html.querySelectorAll('.flash-message')).map(el => el.textContent.trim());
            throw new Error(messages.join('\n') || '檔案未產生，請確認所選月份有薪資資料後重試。');
        }
        const blob = await response.blob();
        const filename = disposition.match(/filename\*=UTF-8''([^;]+)/i);
        const plainFilename = disposition.match(/filename="?([^";]+)"?/i);
        const link = document.createElement('a');
        const objectUrl = URL.createObjectURL(blob);
        link.href = objectUrl;
        link.download = filename ? decodeURIComponent(filename[1]) : (plainFilename?.[1] || '薪資檔案');
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
        const summary = response.headers.get('X-Export-Summary');
        if (summary) {
            const result = JSON.parse(decodeURIComponent(summary));
            notice(result.message, result.category);
        } else {
            notice('檔案已產生並開始下載，請確認下載完成後核對檔案內容。', 'success');
        }
    }

    async function downloadForm(form) {
        const restore = busy(form.querySelector('[type="submit"]'), '正在檢查並產生檔案…');
        try {
            const data = new FormData(form);
            if (form.dataset.remittancePreview) {
                const month = `${data.get('year')}${String(data.get('month')).padStart(2, '0')}`;
                const url = new URL(form.dataset.remittancePreview.replace('__MONTH__', month), location.href);
                url.searchParams.set('tenth_date', String(data.get('tenth_date')).replace(/-/g, ''));
                url.searchParams.set('thirtieth_date', String(data.get('thirtieth_date')).replace(/-/g, ''));
                url.searchParams.set('preview', '1');
                const result = await jsonResponse(await fetch(url, { headers }));
                const lines = [`薪資月份：${month}`, ...result.summary.batches.map(batch =>
                    `${batch.label}：${batch.count} 人，合計 ${batch.total} 元，轉帳日 ${batch.date}`),
                '金額為 0 的員工不列入。本次僅產生檔案，尚未送交銀行。',
                '請核對日期、帳號及金額，並避免重複上傳已執行的匯款。'];
                notice(lines.join('\n'), 'info');
                if (!confirm(lines.join('\n') + '\n\n確認後產生匯款清冊？')) return;
            }
            await download(form.action, { method: 'POST', body: data });
        } catch (error) { errorNotice(error); }
        finally { restore(); }
    }

    async function importFile(url, data, button, kind) {
        const restore = busy(button, '正在檢查檔案…');
        try {
            data.set('preview', '1');
            const preview = await jsonResponse(await fetch(url, { method: 'POST', body: data, headers }));
            const summary = preview.summary;
            const lines = kind === 'history'
                ? [`本次新增 ${summary.new} 筆、更新 ${summary.updated} 筆、跳過 ${summary.skipped} 筆。`,
                    '相同員工與月份的既有薪資將被更新；空白儲存格也可能覆蓋原值。',
                    '公司負擔及零元自付保費會依級距表補齊，請核對年度與補助設定。',
                    ...summary.skipped_details, ...summary.warnings]
                : ['本次將完全取代以下年度的級距資料：', ...summary.years.map(year =>
                    `${year} 年：${summary.by_year[String(year)]} 筆`), `共 ${summary.count} 筆。請確認檔案完整。`];
            notice(lines.join('\n'), 'warning', 'import-preview');
            if (!confirm(lines.slice(0, 12).join('\n') + (lines.length > 12 ? '\n完整明細顯示於頁面。' : '') + '\n\n確認後執行匯入？')) return;
            data.set('preview', '0');
            data.set('confirmation', preview.token);
            if (button) button.textContent = '正在匯入，請勿關閉…';
            const result = await jsonResponse(await fetch(url, { method: 'POST', body: data, headers }));
            navigating = true;
            location.assign(result.redirect);
        } catch (error) { errorNotice(error); }
        finally { restore(); }
    }

    function csv(inputId, url, year, kind) {
        const input = document.getElementById(inputId);
        if (!input) return;
        input.addEventListener('change', () => {
            if (!input.files.length) return;
            const data = new FormData();
            data.set('file', input.files[0]);
            data.set('year', year);
            const button = document.querySelector('button[onclick*="csvFile"]');
            importFile(url, data, button, kind).finally(() => { input.value = ''; });
        });
    }

    window.WorkflowFeedback = { notice, reportInsuranceLevels, csv };
    window.addEventListener('beforeunload', event => {
        if (!navigating && dirty()) { event.preventDefault(); event.returnValue = ''; }
    });
    window.addEventListener('pageshow', () => { navigating = false; });

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('form[data-save-feedback]').forEach(form => {
            const state = { baseline: snapshot(form), touched: false, saving: false };
            forms.set(form, state);
            form.addEventListener('workflow-ready', () => {
                if (!state.touched) state.baseline = snapshot(form);
            });
            const touched = () => { state.touched = true; };
            form.addEventListener('input', touched);
            form.addEventListener('change', touched);
            form.querySelectorAll('.numeric-input').forEach(input => {
                input.addEventListener('input', () => input.setCustomValidity(''));
            });
            form.addEventListener('invalid', event => {
                const tab = event.target.closest('.tab-content');
                if (!tab || tab.classList.contains('active') || typeof window.showTab !== 'function') return;
                const button = Array.from(document.querySelectorAll('.nav-link-custom')).find(el =>
                    (el.getAttribute('onclick') || '').includes(`'${tab.id}'`));
                if (button) window.showTab(button, tab.id);
            }, true);
            form.addEventListener('submit', async event => {
                if (event.defaultPrevented) return;
                event.preventDefault();
                if (state.saving) return;
                if (!form.reportValidity()) return;
                state.saving = true;
                const restore = busy(form.querySelector('[type="submit"]'), '儲存中，請稍候…');
                try {
                    const result = await jsonResponse(await fetch(form.action, {
                        method: 'POST', body: new FormData(form), headers,
                    }));
                    if (!result.redirect) throw new Error('未取得儲存結果，請確認資料後重試。');
                    navigating = true;
                    location.assign(result.redirect);
                } catch (error) { errorNotice(error); }
                finally { state.saving = false; restore(); }
            });
        });
        document.addEventListener('click', event => {
            const link = event.target.closest('a[href]');
            if (!link || link.hasAttribute('download') || event.ctrlKey || event.metaKey || event.shiftKey || link.target === '_blank') return;
            if (link.getAttribute('href').startsWith('#')) return;
            if (!link.hasAttribute('data-download-link') && dirty()) {
                if (!confirm('您有尚未儲存的資料，確定要離開嗎？')) {
                    event.preventDefault();
                    return;
                }
                navigating = true;
            }
            if (link.hasAttribute('data-download-link')) {
                event.preventDefault();
                download(link.href).catch(errorNotice);
            }
        });
        document.querySelectorAll('form[data-download-form]').forEach(form => {
            form.addEventListener('submit', event => {
                event.preventDefault();
                if (form.dataset.downloading) return;
                form.dataset.downloading = '1';
                downloadForm(form).finally(() => { delete form.dataset.downloading; });
            });
        });
        const history = document.getElementById('importForm');
        history?.addEventListener('submit', event => {
            event.preventDefault();
            if (history.dataset.importing) return;
            if (!document.getElementById('fileInput').files.length) {
                errorNotice(new Error('請先選取 Excel 檔案。'));
                return;
            }
            history.dataset.importing = '1';
            importFile(history.action, new FormData(history), history.querySelector('[type="submit"]'), 'history')
                .finally(() => { delete history.dataset.importing; });
        });
    });
})();
