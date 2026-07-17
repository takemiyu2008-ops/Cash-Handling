# クラウド同期の E2E テスト（Playwright + Firebase RTDB モック）
#
# 実行: python3 tests/test_sync.py
#
# - 回帰: 同期未設定時はローカル専用で従来どおり動き、Firebase への通信が一切ないこと
# - 同期: モック RTDB（GET/PUT/if-match/412/空オブジェクト脱落を再現）に対して
#   2端末（別ブラウザコンテキスト）でマージ・オフライン復帰・競合・初期化伝播を検証

import http.server
import json
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765
FAKE_DB = 'https://mock-rtdb.asia-southeast1.firebasedatabase.app'
PASSCODE = 'test-pass-1234'

passed = []
failed = []


def check(name, cond, detail=''):
    if cond:
        passed.append(name)
        print(f'  ok: {name}')
    else:
        failed.append(name)
        print(f'  NG: {name} {detail}')


# ---------- モック RTDB ----------

def fb_clean(v):
    """Firebase RTDB は空オブジェクト・空配列・null を保存しない仕様を再現する"""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            c = fb_clean(x)
            if c is not None:
                out[k] = c
        return out or None
    if isinstance(v, list):
        out = [fb_clean(x) for x in v]
        out = [x for x in out if x is not None]
        return out or None
    return v


class MockRTDB:
    def __init__(self):
        self.node = None
        self.etag = 'null-etag'
        self.counter = 0
        self.offline = False
        self.force_412 = 0
        self.put_count = 0

    def cors(self, extra=None):
        h = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, PUT, OPTIONS',
            'Access-Control-Allow-Headers': '*',
            'Access-Control-Expose-Headers': 'ETag',
            'Content-Type': 'application/json',
        }
        if extra:
            h.update(extra)
        return h

    def handle(self, route, request):
        if request.method == 'OPTIONS':
            route.fulfill(status=204, headers=self.cors())
            return
        if self.offline:
            route.abort()
            return
        # 注意: Playwright sync API は単一スレッド内でグリーンレット切替するため、
        # ここで threading.Lock を使うとデッドロックする。状態更新は fulfill 前に
        # 純Pythonで済ませているので、ロックなしで整合する。
        m = re.match(r'.*firebasedatabase\.app/(.*?)\.json', request.url)
        segs = m.group(1).split('/')  # ['stores', '<id>', ...]
        if request.method == 'GET':
            val = self.node
            for seg in segs[2:]:
                val = val.get(seg) if isinstance(val, dict) else None
            headers = self.cors()
            if request.headers.get('x-firebase-etag'):
                headers['ETag'] = self.etag
            route.fulfill(status=200, headers=headers, body=json.dumps(val))
        elif request.method == 'PUT':
            if self.force_412 > 0:
                self.force_412 -= 1
                route.fulfill(status=412, headers=self.cors({'ETag': self.etag}), body='{}')
                return
            im = request.headers.get('if-match')
            if im is not None and im != self.etag:
                route.fulfill(status=412, headers=self.cors({'ETag': self.etag}), body='{}')
                return
            self.node = fb_clean(json.loads(request.post_data))
            self.counter += 1
            self.etag = f'etag-{self.counter}'
            self.put_count += 1
            route.fulfill(status=200, headers=self.cors(), body='null')
        else:
            route.fulfill(status=405, headers=self.cors(), body='{}')


# ---------- テスト補助 ----------

def wait_until(page, fn, timeout=8.0, interval=0.15):
    """条件成立まで待つ。sleep ではなく page.wait_for_timeout で待つことで
    Playwright のイベントループを回し、モック RTDB のルートハンドラを動かす"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        page.wait_for_timeout(interval * 1000)
    return False


def open_app(browser, url, db=None):
    ctx = browser.new_context()
    if db is not None:
        ctx.route('**/*.firebasedatabase.app/**', db.handle)
    page = ctx.new_page()
    page.on('dialog', lambda d: d.accept())
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(url)
    return ctx, page, errors


def connect(page):
    page.click('#syncChip')
    page.fill('#syncPasscode', PASSCODE)
    page.click('#syncConnect')
    page.wait_for_selector('.sync-chip.ok', timeout=8000)


def add_entry(page, denom, count, kind=None):
    page.click('nav.tabs button[data-tab="entry"]')
    if kind:
        page.select_option('#entryType', kind)
    page.fill(f'#entry-{denom}', str(count))
    page.click('#entrySubmit')


def header_total(page):
    return page.text_content('#headerTotal').strip()


def get_state(page):
    return page.evaluate("JSON.parse(localStorage.getItem('kinko-cash-v1'))")


def trigger_sync(page):
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


# ---------- テスト本体 ----------

def test_local_regression(browser, base_url):
    print('▼ 回帰: 同期未設定（素の index.html）はローカル専用で動く')
    ctx, page, errors = open_app(browser, base_url + '/index.html')
    firebase_requests = []
    page.on('request', lambda r: firebase_requests.append(r.url)
            if 'firebasedatabase.app' in r.url else None)

    check('チップが「同期未設定」', page.text_content('#syncChip') == '同期未設定')

    # 入金 → 残高反映
    add_entry(page, 10000, 2)
    check('入金 20,000円 が反映', header_total(page) == '20,000')

    # 実査（棒金1本 + バラ3枚の 100円 = 53枚 → +5,300円）
    page.click('nav.tabs button[data-tab="audit"]')
    page.fill('#audit-10000', '2')
    page.fill('#audit-roll-100', '1')
    page.fill('#audit-100', '3')
    page.click('#auditSubmit')
    check('実査調整後の残高', header_total(page) == '25,300')

    # 取消（逆方向の取引が追加される）
    page.click('nav.tabs button[data-tab="history"]')
    page.click('.hist-item button.undo >> nth=0')  # 最新（実査調整）を取消
    check('取消後の残高が戻る', header_total(page) == '20,000')
    s = get_state(page)
    reversed_tx = next(t for t in s['transactions'] if t.get('reversedBy'))
    rev_tx = next(t for t in s['transactions'] if t.get('reverses'))
    check('取消の相互参照が uid',
          isinstance(reversed_tx['reversedBy'], str) and rev_tx['reverses'] == reversed_tx['uid'],
          str(reversed_tx))

    # リロードで永続化
    page.reload()
    check('リロード後も残高が残る', header_total(page) == '20,000')

    check('Firebase への通信ゼロ', len(firebase_requests) == 0, str(firebase_requests[:3]))
    check('ページエラーなし（回帰）', not errors, str(errors[:2]))
    ctx.close()


def test_sync_scenarios(browser, base_url):
    url = base_url + '/synced.html'
    db = MockRTDB()

    print('▼ A: 端末1の記録が端末2に伝わる')
    ctx1, page1, err1 = open_app(browser, url, db)
    connect(page1)
    add_entry(page1, 1000, 2)
    check('A: PUT がクラウドに到達', wait_until(page1,
        lambda: db.node and len(db.node['data'].get('transactions', [])) == 1))

    ctx2, page2, err2 = open_app(browser, url, db)
    connect(page2)
    check('A: 端末2が接続時に受信', wait_until(page2, lambda: header_total(page2) == '2,000'))

    add_entry(page1, 5000, 1)
    check('A: 端末1の追加が PUT される', wait_until(page1,
        lambda: len(db.node['data'].get('transactions', [])) == 2))
    trigger_sync(page2)
    check('A: 端末2が visibilitychange で受信', wait_until(page2, lambda: header_total(page2) == '7,000'))

    print('▼ B: 両端末オフラインで別取引 → 復帰後に統合される')
    db.offline = True
    add_entry(page1, 500, 1)
    add_entry(page2, 100, 3)
    page1.wait_for_selector('.sync-chip.error, .sync-chip.offline', timeout=8000)
    db.offline = False
    trigger_sync(page1)
    check('B: 端末1が復帰同期', wait_until(page1, lambda: 'sync-chip ok' in page1.get_attribute('#syncChip', 'class')))
    def both_synced():
        # 端末2の統合結果を端末1が受け取るまで、双方の同期を促しながら待つ
        trigger_sync(page1)
        trigger_sync(page2)
        return header_total(page1) == '7,800' and header_total(page2) == '7,800'
    check('B: 両端末の残高が一致（7,800円）', wait_until(page2, both_synced, interval=0.5),
        f'p1={header_total(page1)} p2={header_total(page2)}')
    s1, s2 = get_state(page1), get_state(page2)
    ids = [t['id'] for t in s1['transactions']]
    check('B: 取引4件・idユニーク', len(s1['transactions']) == 4 and sorted(ids) == [1, 2, 3, 4],
          str(ids))
    check('B: 両端末の取引 uid 集合が一致',
          {t['uid'] for t in s1['transactions']} == {t['uid'] for t in s2['transactions']})

    print('▼ C: PUT の 412 競合 → 再取得マージで成功する')
    before = db.put_count
    db.force_412 = 1
    add_entry(page1, 10, 1)
    check('C: 412 後にリトライ PUT が成功', wait_until(page1,
        lambda: db.put_count > before and len(db.node['data'].get('transactions', [])) == 5))
    check('C: チップが同期済みに戻る', wait_until(page1,
        lambda: 'sync-chip ok' in page1.get_attribute('#syncChip', 'class')))

    print('▼ D: 照合一致（delta:{}）が空オブジェクト脱落込みで往復しても壊れない')
    page1.click('nav.tabs button[data-tab="audit"]')
    page1.click('#auditPrefill')  # 帳簿どおり → 過不足なし → delta:{}
    page1.click('#auditSubmit')
    check('D: 照合一致が PUT される', wait_until(page1,
        lambda: len(db.node['data'].get('transactions', [])) == 6))
    audit_tx = db.node['data']['transactions'][-1]
    check('D: モック上で delta が脱落している', 'delta' not in audit_tx, str(audit_tx))
    trigger_sync(page2)
    check('D: 端末2が受信してもクラッシュしない', wait_until(page2,
        lambda: len(get_state(page2)['transactions']) == 6))
    page2.click('nav.tabs button[data-tab="history"]')
    check('D: 端末2の履歴に実査調整が表示', '実査調整' in page2.text_content('#historyList'))
    check('D: 残高不変（7,810円）', header_total(page2) == '7,810', header_total(page2))

    print('▼ E: 端末1で全データ初期化 → 端末2も空になる（復活なし）')
    gen_before = db.node['data']['gen']
    page1.click('nav.tabs button[data-tab="history"]')
    page1.click('#resetAll')
    check('E: クラウドが新世代の空データになる', wait_until(page1,
        lambda: db.node['data']['gen'] != gen_before and not db.node['data'].get('transactions')))
    trigger_sync(page2)
    check('E: 端末2も空に追従', wait_until(page2,
        lambda: header_total(page2) == '0' and not get_state(page2)['transactions']))
    trigger_sync(page2)
    page2.wait_for_timeout(1500)
    check('E: 取引が復活しない', not db.node['data'].get('transactions'))

    print('▼ F: 両端末に既存データがある状態で接続 → 和集合で統合')
    dbf = MockRTDB()
    ctxa, pagea, erra = open_app(browser, url, dbf)
    add_entry(pagea, 1000, 1)   # 未接続でローカル記録
    connect(pagea)
    check('F: 端末Aの初期アップロード', wait_until(pagea,
        lambda: dbf.node and len(dbf.node['data'].get('transactions', [])) == 1))
    ctxb, pageb, errb = open_app(browser, url, dbf)
    add_entry(pageb, 500, 2)    # こちらも未接続でローカル記録
    connect(pageb)              # 「統合しますか？」confirm は自動承諾
    check('F: 端末Bで統合結果（2,000円）', wait_until(pageb, lambda: header_total(pageb) == '2,000'),
          header_total(pageb))
    check('F: 端末Aにも伝播', wait_synced_total(pagea, '2,000'), header_total(pagea))

    for name, errs in [('端末1', err1), ('端末2', err2), ('端末A', erra), ('端末B', errb)]:
        check(f'ページエラーなし（{name}）', not errs, str(errs[:2]))
    ctx1.close(); ctx2.close(); ctxa.close(); ctxb.close()


def wait_synced_total(page, total, timeout=8.0):
    def cond():
        trigger_sync(page)
        return header_total(page) == total
    return wait_until(page, cond, timeout, 0.4)


def main():
    # 配信用ディレクトリ: 素の index.html と、SYNC_DB_URL を注入した synced.html
    srv_dir = tempfile.mkdtemp(prefix='kinko-test-')
    src = (ROOT / 'index.html').read_text(encoding='utf-8')
    pat = re.compile(r"const SYNC_DB_URL = '[^']*';")
    assert pat.search(src), 'SYNC_DB_URL 定数が見つかりません'
    # 回帰テスト用は URL を空に（ローカル専用動作）、同期テスト用はモック URL に差し替える
    (Path(srv_dir) / 'index.html').write_text(
        pat.sub("const SYNC_DB_URL = '';", src), encoding='utf-8')
    (Path(srv_dir) / 'synced.html').write_text(
        pat.sub(f"const SYNC_DB_URL = '{FAKE_DB}';", src), encoding='utf-8')

    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=srv_dir, **kw)
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base_url = f'http://127.0.0.1:{PORT}'

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            test_local_regression(browser, base_url)
            test_sync_scenarios(browser, base_url)
            browser.close()
    finally:
        httpd.shutdown()
        shutil.rmtree(srv_dir, ignore_errors=True)

    print(f'\n結果: {len(passed)} passed / {len(failed)} failed')
    if failed:
        print('失敗:', *failed, sep='\n  - ')
        sys.exit(1)


if __name__ == '__main__':
    main()
