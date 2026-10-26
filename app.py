import time
import re
import os
import json
from datetime import datetime, timedelta
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import concurrent.futures
import urllib3

urllib3.disable_warnings()

# ⚡ 強制鎖定 Python 執行環境為香港時間 (UTC+8)
os.environ["TZ"] = "Asia/Hong_Kong"
if hasattr(time, "tzset"):
    time.tzset()

# === Telegram 認證資訊 (由 GitHub Secrets 安全注入) ===
TELEGRAM_TOKEN = os.environ.get("TG_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")

# ⚡ 鎖定為 True：初盤與即場凱利均採用「即場市場平均勝率」計算
USE_TITAN_KELLY_MODE = True
STATE_FILE = "recorded_state.json"
recorded_matches = {}

GAME_ARRAY_RE = re.compile(r'game=Array\((.*?)\);', re.DOTALL)
GAME_ITEM_RE = re.compile(r'"(.*?)"')
TIME_EXTRACT_RE = re.compile(r'(\d{2}:\d{2})')
SCORE_RE = re.compile(r'\d+\s*-\s*\d+')
TR_ID_RE = re.compile(r'^tr1_')
FINISHED_KEYWORDS = ("完", "推", "消", "斬", "斩", "斷", "断", "待", "異", "异", "FT", "End")

def create_robust_session():
    s = requests.Session()
    s.headers.update({
        "Referer": "https://live.titan007.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Connection": "keep-alive"
    })
    retry_strategy = Retry(total=2, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
    adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=retry_strategy)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

session = create_robust_session()

def load_state():
    global recorded_matches
    if not os.path.isfile(STATE_FILE): return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        for m_id, data in raw_data.items():
            recorded_matches[m_id] = {
                "state": data["state"],
                "live_str": data["live_str"],
                "updated_at": datetime.fromisoformat(data["updated_at"])
            }
    except Exception:
        pass

def save_state():
    try:
        serializable = {
            m_id: {
                "state": data["state"],
                "live_str": data["live_str"],
                "updated_at": data["updated_at"].isoformat()
            }
            for m_id, data in recorded_matches.items()
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 儲存狀態失敗: {e}")

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = [text[i:i+3800] for i in range(0, len(text), 3800)]
    for chunk in chunks:
        payload = {"chat_id": CHAT_ID, "text": chunk}
        for _ in range(2):
            try:
                res = session.post(url, json=payload, timeout=10)
                if res.status_code == 200:
                    break
            except Exception:
                time.sleep(1)

def cleanup_old_records(current_time):
    expired_ids = [m_id for m_id, data in recorded_matches.items() if current_time - data.get('updated_at', current_time) > timedelta(hours=24)]
    for m_id in expired_ids: del recorded_matches[m_id]

def process_single_match(match_info):
    match_id = match_info['match_id']
    odds_url = f"https://1x2d.titan007.com/{match_id}.js?t={int(time.time()*1000)}"
    try:
        res = session.get(odds_url, timeout=(3.5, 4.5))
        if res.status_code != 200: return None
        res.encoding = 'utf-8'
        js_text = res.text
    except Exception: return None
        
    game_data_match = GAME_ARRAY_RE.search(js_text)
    if not game_data_match: return None
    games = GAME_ITEM_RE.findall(game_data_match.group(1))
    if not games: return None
    
    pinna_row = None
    sumInitProbH, sumInitProbD, sumInitProbA, init_count = 0.0, 0.0, 0.0, 0
    sumLiveProbH, sumLiveProbD, sumLiveProbA, live_count = 0.0, 0.0, 0.0, 0
    
    for g_item in games:
        f = g_item.split('|')
        if len(f) < 13: continue
        if 'Pinna' in f[2]: pinna_row = f
        try:
            ih, id_odds, ia = float(f[3]), float(f[4]), float(f[5])
            if ih > 0 and id_odds > 0 and ia > 0:
                init_ret = 1.0 / (1.0/ih + 1.0/id_odds + 1.0/ia)
                sumInitProbH += init_ret / ih
                sumInitProbD += init_ret / id_odds
                sumInitProbA += init_ret / ia
                init_count += 1
            lh, ld, la = float(f[10] or ih), float(f[11] or id_odds), float(f[12] or ia)
            if lh > 0 and ld > 0 and la > 0:
                live_ret = 1.0 / (1.0/lh + 1.0/ld + 1.0/la)
                sumLiveProbH += live_ret / lh
                sumLiveProbD += live_ret / ld
                sumLiveProbA += live_ret / la
                live_count += 1
        except Exception: continue
            
    if not pinna_row or init_count == 0 or live_count == 0: return None
    
    try:
        pInitH, pInitD, pInitA = float(pinna_row[3]), float(pinna_row[4]), float(pinna_row[5])
        pLiveH, pLiveD, pLiveA = float(pinna_row[10] or pInitH), float(pinna_row[11] or pInitD), float(pinna_row[12] or pInitA)
        
        avgLiveProbH, avgLiveProbD, avgLiveProbA = sumLiveProbH / live_count, sumLiveProbD / live_count, sumLiveProbA / live_count
        if USE_TITAN_KELLY_MODE:
            avgInitProbH, avgInitProbD, avgInitProbA = avgLiveProbH, avgLiveProbD, avgLiveProbA
        else:
            avgInitProbH, avgInitProbD, avgInitProbA = sumInitProbH / init_count, sumInitProbD / init_count, sumInitProbA / init_count
        
        kInitH, kInitD, kInitA = round(pInitH * avgInitProbH, 2), round(pInitD * avgInitProbD, 2), round(pInitA * avgInitProbA, 2)
        kLiveH, kLiveD, kLiveA = round(pLiveH * avgLiveProbH, 2), round(pLiveD * avgLiveProbD, 2), round(pLiveA * avgLiveProbA, 2)
        
        minInitK = min(kInitH, kInitD, kInitA)
        hits, is_qualifying = [], False
        
        if kInitH == minInitK and kLiveH < kInitH and pLiveH < pInitH: hits.append(f"主({kInitH:.2f}➔{kLiveH:.2f})")
        if kInitD == minInitK and kLiveD < kInitD and pLiveD < pInitD: hits.append(f"和({kInitD:.2f}➔{kLiveD:.2f})")
        if kInitA == minInitK and kLiveA < kInitA and pLiveA < pInitA: hits.append(f"客({kInitA:.2f}➔{kLiveA:.2f})")
            
        if hits: is_qualifying = True
        
        # ==========================================
        # ⚡ 僅在「凱利達標」時，才觸發抓取平博亞盤 (極速省流)
        # ==========================================
        asian_str = "--"
        asian_line_change = ""
        
        if is_qualifying:
            asian_url = f"https://vip.titan007.com/AsianOdds_n.aspx?id={match_id}"
            try:
                a_res = session.get(asian_url, timeout=(3.5, 4.5))
                if a_res.status_code == 200:
                    a_res.encoding = 'gb2312'
                    a_html = a_res.text
                    found_asian = False
                    
                    game_data_match_a = GAME_ARRAY_RE.search(a_html)
                    if game_data_match_a:
                        games_a = GAME_ITEM_RE.findall(game_data_match_a.group(1))
                        for g_item in games_a:
                            f_a = g_item.split('|')
                            if any('平博' in val or 'Pinnacle' in val or 'Pinna' in val for val in f_a):
                                name_idx = next(i for i, v in enumerate(f_a) if '平博' in v or 'Pinnacle' in v or 'Pinna' in v)
                                if len(f_a) >= name_idx + 7:
                                    init_h, init_l, init_a = f_a[name_idx+1], f_a[name_idx+2], f_a[name_idx+3]
                                    live_h, live_l, live_a = f_a[name_idx+4], f_a[name_idx+5], f_a[name_idx+6]
                                    asian_str = f"初[{init_h} / {init_l} / {init_a}] ➔ 即[{live_h} / {live_l} / {live_a}]"
                                    asian_line_change = f"(盤變: {init_l}➔{live_l})" if init_l != live_l else ""
                                    found_asian = True
                                break
                    
                    if not found_asian:
                        soup_a = BeautifulSoup(a_html, 'html.parser')
                        for tr in soup_a.find_all('tr'):
                            tds = [td.get_text(strip=True) for td in tr.find_all('td')]
                            if any('平博' in t or 'Pinnacle' in t or 'Pinna' in t for t in tds):
                                n_idx = next(i for i, v in enumerate(tds) if '平博' in v or 'Pinnacle' in v or 'Pinna' in v)
                                if len(tds) >= n_idx + 7:
                                    init_h, init_l, init_a = tds[n_idx+1], tds[n_idx+2], tds[n_idx+3]
                                    live_h, live_l, live_a = tds[n_idx+4], tds[n_idx+5], tds[n_idx+6]
                                    asian_str = f"初[{init_h} / {init_l} / {init_a}] ➔ 即[{live_h} / {live_l} / {live_a}]"
                                    asian_line_change = f"(盤變: {init_l}➔{live_l})" if init_l != live_l else ""
                                break
            except Exception:
                pass
        # ==========================================

        return {
            'match_id': match_id,
            'initStr': f"{kInitH:.2f}/{kInitD:.2f}/{kInitA:.2f}",
            'liveStr': f"{kLiveH:.2f}/{kLiveD:.2f}/{kLiveA:.2f}",
            'hits_str': " ".join(hits) if hits else "--",
            'asian_str': asian_str,
            'asian_line_change': asian_line_change,
            'is_qualifying': is_qualifying,
            'info': match_info
        }
    except Exception: return None

def fetch_main_page_html():
    options = Options()
    options.page_load_strategy = 'eager'
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-features=VizDisplayCompositor")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    
    for attempt in range(1, 4):
        driver = None
        try:
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(20)
            
            try:
                driver.get("https://live.titan007.com/indexall_big.aspx")
            except Exception:
                pass

            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//tr[starts-with(@id, 'tr1_')]"))
                )
            except Exception:
                pass

            html = driver.page_source
            if html and "tr1_" in html:
                return html
        except Exception:
            pass
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
        time.sleep(2)
    return ""

def run_scraper_once():
    start_ts = time.time()
    current_scan_time = datetime.now()
    cleanup_old_records(current_scan_time)

    page_html = fetch_main_page_html()
    if not page_html:
        return

    try:
        soup = BeautifulSoup(page_html, 'html.parser')
        rows = soup.find_all('tr', id=TR_ID_RE)
        
        match_infos = []
        for tr in rows:
            if str(tr.get('state', '')).strip().startswith('-'): continue
            match_id = tr['id'].replace('tr1_', '')
            tds = tr.find_all('td')
            status_text = (tr.find(id=f"time_{match_id}") or (tds[3] if len(tds)>3 else None))
            status_text = status_text.text.strip() if status_text else ""
            if any(k in status_text for k in FINISHED_KEYWORDS): continue

            home = (tr.find(id=f"team1_{match_id}").text.strip() if tr.find(id=f"team1_{match_id}") else "")
            away = (tr.find(id=f"team2_{match_id}").text.strip() if tr.find(id=f"team2_{match_id}") else "")
            
            raw_match_time = (tr.find(id=f"mt_{match_id}").text.strip() if tr.find(id=f"mt_{match_id}") else (tds[2].text.strip() if len(tds)>2 else ""))
            
            time_m = TIME_EXTRACT_RE.search(raw_match_time)
            match_dt = current_scan_time
            formatted_time = raw_match_time
            if time_m:
                hour, minute = map(int, time_m.group(1).split(':'))
                match_dt = current_scan_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if match_dt > current_scan_time + timedelta(hours=12): match_dt -= timedelta(days=1)
                elif match_dt < current_scan_time - timedelta(hours=12): match_dt += timedelta(days=1)
                formatted_time = match_dt.strftime('%Y-%m-%d %H:%M')

            if match_dt + timedelta(minutes=115) < current_scan_time: continue
                
            match_infos.append({'match_id': match_id, 'league': tds[1].text.strip() if len(tds)>1 else "", 'time': formatted_time, 'match_dt': match_dt, 'home': home, 'away': away})
            
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=45) as executor:
            future_to_match = {executor.submit(process_single_match, info): info for info in match_infos}
            for future in concurrent.futures.as_completed(future_to_match, timeout=45):
                res = future.result()
                if res: results.append(res)
                    
        valid_alerts = []
        for r in results:
            m_id, is_qualifying, live_str = r['match_id'], r['is_qualifying'], r['liveStr']
            if is_qualifying:
                # 修正：字眼改為「凱利」與「最低項跌」
                msg = f"🏆 {r['info']['league']} | ⏱ {r['info']['time']}\n"
                msg += f"⚽ {r['info']['home']} vs {r['info']['away']}\n"
                msg += f"📊 凱利: 初[{r['initStr']}] ➔ 即[{live_str}]\n"
                msg += f"🔻 最低項跌: {r['hits_str']}\n"
                if r['asian_str'] != "--":
                    msg += f"📐 亞盤: {r['asian_str']} {r['asian_line_change']}".strip()
                else:
                    msg += "📐 亞盤: 暫無平博數據"

                valid_alerts.append({'match_dt': r['info']['match_dt'], 'msg': msg})
                
                prev = recorded_matches.get(m_id, {})
                if not prev or prev.get('state') == 'unqualifying' or prev.get('live_str') != live_str:
                    recorded_matches[m_id] = {'state': 'qualifying', 'live_str': live_str, 'updated_at': current_scan_time}
                else: recorded_matches[m_id]['updated_at'] = current_scan_time
            else:
                if m_id in recorded_matches and recorded_matches[m_id]['state'] == 'qualifying':
                    recorded_matches[m_id] = {'state': 'unqualifying', 'live_str': live_str, 'updated_at': current_scan_time}

        save_state()
        
        if valid_alerts:
            valid_alerts.sort(key=lambda x: (x['match_dt'] < current_scan_time, x['match_dt']))
            send_telegram_message("🚨 Pinna 最新凱利達標：\n\n" + "\n\n".join([x['msg'] for x in valid_alerts]))

    except Exception:
        pass

if __name__ == "__main__":
    load_state()
    run_scraper_once()
