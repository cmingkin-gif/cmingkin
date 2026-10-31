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

# === 你的專屬 Telegram 資訊 ===
TELEGRAM_TOKEN = os.environ.get("TG_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")

STATE_FILE = "recorded_state.json"
recorded_matches = {}

FINISHED_KEYWORDS = ("完", "FT", "End", "推", "消", "斬", "斩", "斷", "断")

def load_state():
    """讀取主腳本留下來的 JSON 狀態檔"""
    global recorded_matches
    if not os.path.isfile(STATE_FILE): 
        print("⚠️ 找不到 recorded_state.json，可能主腳本尚未產生記錄。")
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        for m_id, data in raw_data.items():
            recorded_matches[m_id] = {
                "state": data.get("state"),
                "live_str": data.get("live_str"),
                "updated_at": datetime.fromisoformat(data["updated_at"]),
                "league": data.get("league", "未知聯賽"),
                "home": data.get("home", "主隊"),
                "away": data.get("away", "客隊")
            }
        print(f"📂 成功載入 {len(recorded_matches)} 場賽事記錄。")
    except Exception as e:
        print(f"⚠️ 讀取狀態檔失敗: {e}")

def save_state():
    """更新並儲存 JSON 狀態檔 (標記已結算的賽事)"""
    try:
        serializable = {
            m_id: {
                "state": data["state"],
                "live_str": data["live_str"],
                "updated_at": data["updated_at"].isoformat(),
                "league": data.get("league", ""),
                "home": data.get("home", ""),
                "away": data.get("away", "")
            }
            for m_id, data in recorded_matches.items()
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 儲存狀態檔失敗: {e}")

def send_telegram_message(text):
    """發送結算報告去 Telegram"""
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

def fetch_main_page_html():
    """使用 Selenium 抓取球探網最新實時比分頁面"""
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
            print(f"🌐 [第 {attempt}/3 次嘗試] 正在連線至球探網獲取完場比分...", flush=True)
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(20)
            driver.get("https://live.titan007.com/indexall_big.aspx")
            
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//tr[starts-with(@id, 'tr1_')]")))
            html = driver.page_source
            if html and "tr1_" in html:
                return html
        except Exception as e:
            print(f"⚠️ 第 {attempt} 次獲取頁面失敗: {e}")
        finally:
            if driver:
                try: driver.quit()
                except: pass
        time.sleep(2)
    return ""

def check_results():
    print(f"\n🔍 [完場比分結算器] 啟動時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    load_state()
    
    # 🎯 篩選出之前達標但仲未結算嘅賽事
    pending_matches = {m: d for m, d in recorded_matches.items() if d['state'] == 'qualifying'}
    
    if not pending_matches:
        print("✅ 目前沒有需要等待結算的達標賽事，任務結束。")
        return
        
    print(f"🎯 發現 {len(pending_matches)} 場待結算賽事，正在爬取最新賽果...")
    html = fetch_main_page_html()
    if not html:
        print("❌ 無法獲取網頁資料，結束任務。")
        return
        
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
            
            # 確保賽事已完場
            if any(k in status_text for k in FINISHED_KEYWORDS):
                score = None
                for td in tds:
                    # 尋找類似 "2-1" 或 "0 - 0" 的比分格式
                    if re.match(r'^\d+\s*-\s*\d+$', td.text.strip()):
                        score = td.text.strip()
                        break
                
                if score:
                    m_data = pending_matches[m_id]
                    lg = m_data.get('league', '未知聯賽')
                    hm = m_data.get('home', '主隊')
                    aw = m_data.get('away', '客隊')
                    
                    msg = f"🏁 賽果結算\n🏆 {lg}\n⚽ {hm} {score} {aw}\n📊 賽前終盤: {m_data['live_str']}"
                    alerts.append(msg)
                    # 標記為已結算，防止下次再 send
                    recorded_matches[m_id]['state'] = 'settled'
                    print(f"✅ 結算成功: {hm} {score} {aw}")

    # 🧹 清理卡住超過 48 小時的無結果賽事 (防止 JSON 檔案無限膨脹)
    for m_id, data in recorded_matches.items():
        if data['state'] == 'qualifying' and current_time - data['updated_at'] > timedelta(hours=48):
            recorded_matches[m_id]['state'] = 'expired'
            print(f"🗑️ 賽事 {m_id} 超過 48 小時未結算，已標記為過期 (expired)。")

    # 📨 發送 Telegram 通知
    if alerts:
        send_telegram_message("🔔 凱利達標賽事完場結算：\n\n" + "\n\n".join(alerts))
        print(f"📨 已發送 {len(alerts)} 場賽果通知至 Telegram！")
    else:
        print("👀 今次掃描未有新完場的達標賽事。")
        
    save_state()
    print("✅ 狀態檔已更新，任務圓滿結束。")

if __name__ == "__main__":
    check_results()
