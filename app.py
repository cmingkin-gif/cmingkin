import time
import re
import os
import json
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import concurrent.futures
import urllib3

urllib3.disable_warnings()
os.environ["TZ"] = "Asia/Hong_Kong"
if hasattr(time, "tzset"): time.tzset()

TELEGRAM_TOKEN = "8814919382:AAEvbUic7bqtk_aXBeu5b6f7HRquJnOCzrs"
CHAT_ID = "746157499"
STATE_FILE = "recorded_state.json"
recorded_matches = {}

GAME_ARRAY_RE = re.compile(r'game=Array\((.*?)\);', re.DOTALL)
GAME_ITEM_RE = re.compile(r'"(.*?)"')
FULL_DATE_TIME_RE = re.compile(r'(\d{1,2})-(\d{1,2})\s*(\d{2}:\d{2})')
DAY_TIME_RE = re.compile(r'(\d{1,2})[日号號]\s*(\d{2}:\d{2})')
TIME_EXTRACT_RE = re.compile(r'(\d{2}:\d{2})')
SCORE_RE = re.compile(r'\d+\s*-\s*\d+')
TR_ID_RE = re.compile(r'^tr1_')
FINISHED_KEYWORDS = ("完", "推", "消", "斬", "斩", "斷", "断", "待", "異", "异", "FT", "End")

def load_state():
    global recorded_matches
    if not os.path.isfile(STATE_FILE):
        with open(STATE_FILE, "w") as f: json.dump({}, f)
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f: raw_data = json.load(f)
        for m_id, data in raw_data.items():
            recorded_matches[m_id] = {
                "state": data["state"], "live_str": data["live_str"],
                "updated_at": datetime.fromisoformat(data["updated_at"])
            }
    except Exception: pass

def save_state():
    try:
        serializable = {
            m_id: {"state": data["state"], "live_str": data["live_str"], "updated_at": data["updated_at"].isoformat()}
            for m_id, data in recorded_matches.items()
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception: pass

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = [text[i:i+3800] for i in range(0, len(text), 3800)]
    for chunk in chunks:
        try: requests.post(url, json={"chat_id": CHAT_ID, "text": chunk}, timeout=5)
        except Exception: pass

def process_single_match(match_info):
    match_id = match_info['match_id']
    odds_url = f"https://1x2d.titan007.com/{match_id}.js?t={int(time.time()*1000)}"
    headers = {"Referer": "https://live.titan007.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"}
    try:
        res = requests.get(odds_url, headers=headers, timeout=(1.5, 2.0))
        if res.status_code != 200: return None
        res.encoding = 'utf-8'
        js_text = res.text
    except Exception: return None
        
    game_data_match = GAME_ARRAY_RE.search(js_text)
    if not game_data_match: return None
    games = GAME_ITEM_RE.findall(game_data_match.group(1))
    if not games: return None
    
    pinna_row = None
    sumInitH, sumInitD, sumInitA, init_count = 0.0, 0.0, 0.0, 0
    sumLiveH, sumLiveD, sumLiveA, live_count = 0.0, 0.0, 0.0, 0
    
    for g_item in games:
        f = g_item.split('|')
        if len(f) < 13: continue
        if 'Pinna' in f[2]: pinna_row = f
        try:
            ih, id_odds, ia = float(f[3]), float(f[4]), float(f[5])
            if ih > 0 and id_odds > 0 and ia > 0:
                sumInitH += ih; sumInitD += id_odds; sumInitA += ia; init_count += 1
            lh, ld, la = float(f[10] or ih), float(f[11] or id_odds), float(f[12] or ia)
            if lh > 0 and ld > 0 and la > 0:
                sumLiveH += lh; sumLiveD += ld; sumLiveA += la; live_count += 1
        except: continue
            
    if not pinna_row or init_count == 0 or live_count == 0: return None
    
    try:
        pInitH, pInitD, pInitA = float(pinna_row[3]), float(pinna_row[4]), float(pinna_row[5])
        pLiveH, pLiveD, pLiveA = float(pinna_row[10] or pInitH), float(pinna_row[11] or pInitD), float(pinna_row[12] or pInitA)
        
        avgInitH, avgInitD, avgInitA = sumInitH/init_count, sumInitD/init_count, sumInitA/init_count
        avgLiveH, avgLiveD, avgLiveA = sumLiveH/live_count, sumLiveD/live_count, sumLiveA/live_count
        
        init_ret = 1.0 / (1.0/avgInitH + 1.0/avgInitD + 1.0/avgInitA)
        live_ret = 1.0 / (1.0/avgLiveH + 1.0/avgLiveD + 1.0/avgLiveA)
        
        kInitH, kInitD, kInitA = round(pInitH*(init_ret/avgInitH), 2), round(pInitD*(init_ret/avgInitD), 2), round(pInitA*(init_ret/avgInitA), 2)
        kLiveH, kLiveD, kLiveA = round(pLiveH*(live_ret/avgLiveH), 2), round(pLiveD*(live_ret/avgLiveD), 2), round(pLiveA*(live_ret/avgLiveA), 2)
        
        minInitK = min(kInitH, kInitD, kInitA)
        hits = []
        if kInitH == minInitK and kLiveH < kInitH and pLiveH < pInitH: hits.append(f"主({kInitH:.2f}➔{kLiveH:.2f})")
        if kInitD == minInitK and kLiveD < kInitD and pLiveD < pInitD: hits.append(f"和({kInitD:.2f}➔{kLiveD:.2f})")
        if kInitA == minInitK and kLiveA < kInitA and pLiveA < pInitA: hits.append(f"客({kInitA:.2f}➔{kLiveA:.2f})")
            
        if hits:
            return {
                'match_id': match_id, 'initStr': f"{kInitH:.2f}/{kInitD:.2f}/{kInitA:.2f}",
                'liveStr': f"{kLiveH:.2f}/{kLiveD:.2f}/{kLiveA:.2f}", 'hits_str': " ".join(hits),
                'is_qualifying': True, 'info': match_info
            }
        return {'match_id': match_id, 'is_qualifying': False, 'liveStr': f"{kLiveH:.2f}/{kLiveD:.2f}/{kLiveA:.2f}"}
    except: return None

def parse_match_datetime(raw_match_time, current_scan_time, has_started):
    full_m = FULL_DATE_TIME_RE.search(raw_match_time)
    if full_m:
        month, day, hh_mm = int(full_m.group(1)), int(full_m.group(2)), full_m.group(3)
        hour, minute = map(int, hh_mm.split(':'))
        try:
            dt = datetime(current_scan_time.year, month, day, hour, minute)
            return dt.strftime('%Y-%m-%d %H:%M'), dt
        except: pass

    day_m = DAY_TIME_RE.search(raw_match_time)
    if day_m:
        day, hh_mm = int(day_m.group(1)), day_m.group(2)
        hour, minute = map(int, hh_mm.split(':'))
        target_date = current_scan_time
        if day != current_scan_time.day:
            if (current_scan_time + timedelta(days=1)).day == day: target_date += timedelta(days=1)
            elif (current_scan_time - timedelta(days=1)).day == day: target_date -= timedelta(days=1)
        dt = target_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return dt.strftime('%Y-%m-%d %H:%M'), dt

    time_m = TIME_EXTRACT_RE.search(raw_match_time)
    if time_m:
        hh_mm = time_m.group(1)
        hour, minute = map(int, hh_mm.split(':'))
        candidate_dt = current_scan_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if has_started and candidate_dt > current_scan_time + timedelta(minutes=15): candidate_dt -= timedelta(days=1)
        elif not has_started:
            if candidate_dt < current_scan_time - timedelta(minutes=20): candidate_dt += timedelta(days=1)
            elif candidate_dt > current_scan_time + timedelta(hours=18): candidate_dt -= timedelta(days=1)
        return candidate_dt.strftime('%Y-%m-%d %H:%M'), candidate_dt
    return raw_match_time, current_scan_time

def check_titan_matches():
    current_scan_time = datetime.now()
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    
    driver = None
    page_html = ""
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(20)
        driver.get("https://live.titan007.com/indexall_big.aspx")
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.XPATH, "//tr[starts-with(@id, 'tr1_')]")))
        page_html = driver.page_source
    except Exception as e:
        print(f"❌ 網頁讀取失敗: {e}")
        return
    finally:
        if driver: driver.quit()

    soup = BeautifulSoup(page_html, 'html.parser')
    rows = soup.find_all('tr', id=TR_ID_RE)
    if not rows: return
        
    match_infos = []
    for tr in rows:
        if str(tr.get('state', '')).strip().startswith('-'): continue
        match_id = tr['id'].replace('tr1_', '')
        tds = tr.find_all('td')
        
        status_elem = tr.find(id=f"time_{match_id}") or (tds[3] if len(tds) > 3 else None)
        status_text = status_elem.text.strip() if status_elem else ""
        if any(k in status_text for k in FINISHED_KEYWORDS): continue

        home_elem, away_elem = tr.find(id=f"team1_{match_id}"), tr.find(id=f"team2_{match_id}")
        home_team = home_elem.text.strip() if home_elem else ""
        away_team = away_elem.text.strip() if away_elem else ""
        league_name = tds[1].text.strip() if len(tds) > 1 else "未知"
        
        time_elem = tr.find(id=f"mt_{match_id}")
        raw_match_time = time_elem.text.strip() if time_elem else (tds[2].text.strip() if len(tds) > 2 else "")
        score_text = tds[5].text.strip() if len(tds) > 5 else ""
        has_started = bool(status_text) or bool(SCORE_RE.search(score_text))
        
        formatted_match_time, match_dt = parse_match_datetime(raw_match_time, current_scan_time, has_started)
        if match_dt + timedelta(minutes=115) < current_scan_time: continue
            
        match_infos.append({
            'match_id': match_id, 'league': league_name, 'time': formatted_match_time,
            'match_dt': match_dt, 'home': home_team, 'away': away_team
        })
        
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(process_single_match, info): info for info in match_infos}
        for future in concurrent.futures.as_completed(futures, timeout=45):
            try:
                res = future.result()
                if res: results.append(res)
            except: pass
                
    valid_alerts = []
    for r in results:
        m_id, is_qual = r['match_id'], r['is_qualifying']
        if is_qual:
            info = r['info']
            msg = f"🏆 {info['league']} | ⏱ {info['time']}\n⚽ {info['home']} vs {info['away']}\n📊 初[{r['initStr']}] ➔ 即[{r['liveStr']}]\n🔻 最低項跌: {r['hits_str']}"
            valid_alerts.append({'match_dt': info['match_dt'], 'msg': msg})
            recorded_matches[m_id] = {'state': 'qualifying', 'live_str': r['liveStr'], 'updated_at': current_scan_time}
        else:
            if m_id in recorded_matches and recorded_matches[m_id]['state'] == 'qualifying':
                recorded_matches[m_id] = {'state': 'unqualifying', 'live_str': r['liveStr'], 'updated_at': current_scan_time}

    expired_ids = [m for m, d in recorded_matches.items() if current_scan_time - d.get('updated_at', current_scan_time) > timedelta(hours=24)]
    for m in expired_ids: del recorded_matches[m]
    save_state()

    if valid_alerts:
        valid_alerts.sort(key=lambda x: (x['match_dt'] < current_scan_time, x['match_dt']))
        final_msg = "🚨 Pinna 最新凱利達標：\n\n" + "\n\n".join([x['msg'] for x in valid_alerts])
        send_telegram_message(final_msg)
        print(f"✅ 成功發送 {len(valid_alerts)} 場通知！")

if __name__ == "__main__":
    load_state()
    check_titan_matches()
