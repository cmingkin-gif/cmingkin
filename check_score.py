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
import urllib3

urllib3.disable_warnings()

# ⚡ 強制鎖定 Python 執行環境為香港時間 (UTC+8)
os.environ["TZ"] = "Asia/Hong_Kong"
if hasattr(time, "tzset"):
    time.tzset()

TELEGRAM_TOKEN = os.environ.get("TG_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")

STATE_FILE = "recorded_state.json"
recorded_matches = {}

FINISHED_KEYWORDS = ("完", "FT", "End", "推", "消", "斬", "斩", "斷", "断")
PINNACLE_KEYWORDS = ('平博', 'Pinnacle', 'Pinna', '平*')
GAME_ARRAY_RE = re.compile(r'game=Array\((.*?)\);', re.DOTALL)
GAME_ITEM_RE = re.compile(r'"(.*?)"')
USE_TITAN_KELLY_MODE = True

def load_state():
    global recorded_matches
    if not os.path.isfile(STATE_FILE): return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        for m_id, data in raw_data.items():
            recorded_matches[m_id] = {
                "state": data.get("state"),
                "live_str": data.get("live_str", "未知"),
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
    except Exception:
        pass

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = [text[i:i+3800] for i in range(0, len(text), 3800)]
    for chunk in chunks:
        payload = {"chat_id": CHAT_ID, "text": chunk}
        for _ in range(2):
            try:
                if requests.post(url, json=payload, timeout=10).status_code == 200: break
            except Exception:
                time.sleep(1)

def fetch_match_odds(match_id):
    """實時潛入後台，重新計算初盤與凱利跌幅 (無須依賴 app.py)"""
    try:
        odds_url = f"https://1x2d.titan007.com/{match_id}.js?t={int(time.time()*1000)}"
        res = requests.get(odds_url, headers={"Referer": "https://live.titan007.com/"}, timeout=5)
        if res.status_code != 200: return "未知", "--"
        res.encoding = 'utf-8'
        js_text = res.text
        
        game_data_match = GAME_ARRAY_RE.search(js_text)
        if not game_data_match: return "未知", "--"
        games = GAME_ITEM_RE.findall(game_data_match.group(1))
        
        pinna_row = None
        sumInitProbH, sumInitProbD, sumInitProbA, init_count = 0.0, 0.0, 0.0, 0
        sumLiveProbH, sumLiveProbD, sumLiveProbA, live_count = 0.0, 0.0, 0.0, 0
        
        for g_item in games:
            f = g_item.split('|')
            if len(f) < 13: continue
            if any(k in f[2] for k in PINNACLE_KEYWORDS): pinna_row = f
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
                
        if not pinna_row or init_count == 0 or live_count == 0: return "未知", "--"
        
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
        hits = []
        if kInitH == minInitK and kLiveH < kInitH and pLiveH < pInitH: hits.append(f"主({kInitH:.2f}➔{kLiveH:.2f})")
        if kInitD == minInitK and kLiveD < kInitD and pLiveD < pInitD: hits.append(f"和({kInitD:.2f}➔{kLiveD:.2f})")
        if kInitA == minInitK and kLiveA < kInitA and pLiveA < pInitA: hits.append(f"客({kInitA:.2f}➔{kLiveA:.2f})")
            
        initStr = f"{kInitH:.2f}/{kInitD:.2f}/{kInitA:.2f}"
        hits_str = " ".join(hits) if hits else "--"
        
        return initStr, hits_str
    except Exception:
        return "未知", "--"

def fetch_main_page_html():
    options = Options()
    options.page_load_strategy = 'eager'
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    
    for attempt in range(1, 4):
        driver = None
        try:
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(20)
            driver.get("https://live.titan007.com/indexall_big.aspx")
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//tr[starts-with(@id, 'tr1_')]")))
            html = driver.page_source
            if html and "tr1_" in html: return html
        except Exception:
            pass
        finally:
            if driver:
                try: driver.quit()
                except: pass
        time.sleep(2)
    return ""

def check_results():
    load_state()
    
    pending_matches = {m: d for m, d in recorded_matches.items() if d['state'] == 'qualifying'}
    if not pending_matches: 
        print("👀 目前沒有等待結算的賽事。")
        return
        
    html = fetch_main_page_html()
    if not html: return
        
    soup = BeautifulSoup(html, 'html.parser')
    rows = soup.find_all('tr', id=re.compile(r'^tr1_'))
    
    alerts = []
    current_time = datetime.now()
    
    for tr in rows:
        m_id = tr.get('id', '').replace('tr1_', '')
        if m_id in pending_matches:
            tds = tr.find_all('td')
            status_text = (tr.find(id=f"time_{m_id}") or (tds[3] if len(tds)>3 else None))
            status_text = status_text.text.strip() if status_text else ""
            
            if any(k in status_text for k in FINISHED_KEYWORDS):
                score = None
                for td in tds:
                    if re.match(r'^\d+\s*-\s*\d+$', td.text.strip()):
                        score = td.text.strip()
                        break
                
                if score:
                    # 💡 絕招：自己喺網頁抽聯賽名同球隊名，唔靠 app.py
                    league = tds[1].text.strip() if len(tds) > 1 else "未知聯賽"
                    home = tr.find(id=f"team1_{m_id}").text.strip() if tr.find(id=f"team1_{m_id}") else "主隊"
                    away = tr.find(id=f"team2_{m_id}").text.strip() if tr.find(id=f"team2_{m_id}") else "客隊"
                    
                    # 💡 絕招：自己潛入後台重新計初盤同跌幅
                    init_s, hits_s = fetch_match_odds(m_id)
                    live_s = pending_matches[m_id].get('live_str', '未知')
                    
                    msg = f"🏁 賽果結算\n🏆 {league}\n⚽ {home} {score} {away}\n📊 凱利變化: 初[{init_s}] ➔ 終[{live_s}]\n🔻 最低項跌: {hits_s}"
                    alerts.append(msg)
                    recorded_matches[m_id]['state'] = 'settled'
                    print(f"✅ 結算成功: {home} {score} {away}")

    for m_id, data in recorded_matches.items():
        if data['state'] == 'qualifying' and current_time - data['updated_at'] > timedelta(hours=48):
            recorded_matches[m_id]['state'] = 'expired'

    if alerts:
        send_telegram_message("🔔 凱利達標賽事完場結算：\n\n" + "\n\n".join(alerts))
        
    save_state()

if __name__ == "__main__":
    check_results()
