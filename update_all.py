// ================= 設定區域 =================
const CONFIG = {
  // 請確認您的 Token 與 ID 是否正確
  TG_TOKEN: "7810085639:AAGgZizRhKMLbiiKdZYODfNnBunLpR8YMiU", 
  TG_CHAT_ID: "-1002664304447", 
  // GitHub JSON 資料來源
  DATA_URL: "https://raw.githubusercontent.com/Chris701117/tw-institutional-stocker/main/docs/data/top_three_inst_change_5_up.json"
};
// ===========================================

function main() {
  try {
    // ============================================
    // 🕒 超級時段管控 (智慧防擾版)
    // ============================================
    let now = new Date();
    // 取得台北時間的「日期」與「小時」
    let dateStr = Utilities.formatDate(now, "Asia/Taipei", "yyyy-MM-dd");
    let hour = parseInt(Utilities.formatDate(now, "Asia/Taipei", "HH"));

    // ⛔️ 情境 A：深夜休息 (00:00 ~ 08:59) 
    // ★★★ 為了測試，暫時註解掉這段，讓您現在(半夜)也能收到通知 ★★★
    /*
    if (hour < 9) {
      return;
    }
    */

    // ⛔️ 情境 B：下午空窗期 (14:00 ~ 18:59) -> 等待 GitHub 資料更新，暫停執行
    if (hour >= 14 && hour < 19) {
      console.log(`😴 下午休眠時間 (${hour}點)，等待晚間籌碼更新...`);
      return; 
    }

    // ✅ 情境 C：晚上盤後戰報 (19:00 ~ 23:59) -> 【關鍵修改：限制只發一次】
    if (hour >= 19) {
      // 讀取系統紀錄，看今天這份戰報發過沒？
      let scriptProperties = PropertiesService.getScriptProperties();
      let lastRunDate = scriptProperties.getProperty("LAST_NIGHT_REPORT_DATE");
      
      // ★★★ 為了測試，這裡也可以暫時註解掉，確保您現在能收到 ★★★
      /*
      if (lastRunDate === dateStr) {
        console.log("✅ 今日盤後戰報已發送過，不再重複打擾。");
        return; // 直接結束，不再往下執行
      }
      */
    }
    // ============================================


    // 1. 抓取資料
    const response = UrlFetchApp.fetch(CONFIG.DATA_URL);
    const data = JSON.parse(response.getContentText());
    
    // 2. 初始化分類
    let report = { 
      "bothBuy": [],      // 雙資齊買
      "trustBuy": [],     // 投信認養
      "lowHeavyVol": [],  // 💥 盤中爆量
      "highConc": [],     // 籌碼集中
      "trustSell": []     // 投信棄養
    };
    
    let rows = [];

    // 3. 遍歷資料並分類
    data.forEach(stock => {
      let code = stock.code;
      if (code.startsWith("00") || code.length > 4) return; // 排除 ETF

      let total_c = parseFloat(stock.change) || 0; 
      let ratio = parseFloat(stock.three_inst_ratio) || 0;
      let f_diff = parseFloat(stock.foreign_ratio_diff) || 0; 
      let t_diff = parseFloat(stock.trust_ratio_diff) || 0;   
      
      let vol_ratio = parseFloat(stock.vol_ratio) || 0;   
      let price_pos = parseFloat(stock.price_pos) || 0;   

      // --- 條件判斷邏輯 ---

      // A. 🔥 雙資齊買
      if (f_diff > 0 && t_diff > 0) {
        report.bothBuy.push({
          msg: `<b>${code} ${stock.name}</b> (外:+${f_diff.toFixed(2)} 投:+${t_diff.toFixed(2)})`,
          val: f_diff + t_diff 
        });
      }

      // B. 🚀 投信強勢認養
      if (t_diff > 0.01) {
        report.trustBuy.push({
          msg: `<b>${code} ${stock.name}</b> (投增:<code>+${t_diff.toFixed(1)}張</code>)`, // 已修正單位
          val: t_diff
        });
      }

      // C. 💥 盤中預估爆量排行 (前50強)
      // 條件：量比 > 2.5倍 且 位階 < 0.8 (放寬) 且 法人有買
      if (vol_ratio > 2.5 && price_pos < 0.80 && (t_diff > 0 || f_diff > 0)) {
        report.lowHeavyVol.push({
          msg: `<b>${code} ${stock.name}</b> (量比:<code>${vol_ratio.toFixed(1)}倍</code> 位階:<code>${(price_pos*100).toFixed(0)}%</code>)`,
          val: vol_ratio
        });
      }

      // D. 💎 籌碼高度集中
      if (ratio > 30) {
        report.highConc.push({
          msg: `<b>${code} ${stock.name}</b> (佔:<code>${ratio.toFixed(1)}%</code>)`,
          val: ratio
        });
      }

      // E. 🚨 投信棄養警報
      if (t_diff < 0) {
        report.trustSell.push({
          msg: `${code} ${stock.name} (投減:<code>${t_diff.toFixed(1)}張</code>)`, // 已修正單位
          val: t_diff
        });
      }

      rows.push([code, stock.name, total_c, ratio, f_diff, t_diff, vol_ratio]);
    });

    // ============================================
    // 🛡️ 關鍵防呆：盤中舊資料過濾鎖 (維持不動)
    // ============================================
    let isIntraday = (hour >= 9 && hour < 14);
    if (isIntraday && report.lowHeavyVol.length === 0) {
      console.log("⏳ 盤中無爆量股資料，判定為舊資料，跳過。");
      return; 
    }
    // ============================================

    // 4. 發送 Telegram 通知
    sendEnhancedNotification(report);
    
    // 5. 更新 Google Sheet
    updateSheet(rows); 

    // ============================================
    // ✅ 【最後一步】：如果是晚上執行成功，蓋上「已發送章」
    // ============================================
    if (hour >= 19) {
      PropertiesService.getScriptProperties().setProperty("LAST_NIGHT_REPORT_DATE", dateStr);
      console.log(`📝 已記錄：${dateStr} 的盤後報表發送完成。`);
    }

  } catch (e) { 
    console.log("執行錯誤: " + e.message); 
  }
}

function sendEnhancedNotification(report) {
  // --- 時間與時區設定 ---
  let now = new Date();
  let timeString = Utilities.formatDate(now, "Asia/Taipei", "HH:mm"); 
  let parts = timeString.split(':');
  let hour = parseInt(parts[0]); 
  let min = parts[1];            
  
  // 09:00 - 14:00 算盤中
  let isIntraday = (hour >= 9 && hour < 14);
  
  // 🔧 排序與取前 N 名的共用函式
  const getTopList = (arr, limit = 10, desc = true) => {
    return arr.sort((a, b) => desc ? b.val - a.val : a.val - b.val)
              .slice(0, limit)
              .map(item => item.msg);
  };

  let msg = "";

  // ==========================================
  // 🎯 盤中模式：只顯示爆量股 (擴增至 50 檔)
  // ==========================================
  if (isIntraday) {
    msg = "🚨 <b>【盤中即時：量能預估戰報】</b>\n";
    msg += `<i>(更新時間：${hour}:${min}，爆量為全天預估值)</i>\n`;
    
    // 1. 💥 盤中預估爆量排行 (改成顯示 50 檔)
    if (report.lowHeavyVol && report.lowHeavyVol.length > 0) {
      // 👇 修改這裡：參數改為 50，標題也更新
      msg += "\n💥 <b>盤中預估爆量排行 (前50強)：</b>\n" + getTopList(report.lowHeavyVol, 50).join("\n");
    } else {
      msg += "\n💤 目前尚無符合條件的爆量股。";
    }
  
  // ==========================================
  // 📊 盤後模式：顯示完整籌碼分析
  // ==========================================
  } else {
    msg = "📊 <b>【法人籌碼+量價選股戰報】</b>\n";
    msg += "<i>(資料日期：今日收盤，偵測低檔轉強)</i>\n";
    msg += "<i>(排除 ETF，聚焦個股籌碼)</i>\n";

    // 1. 💥 盤後也同步改名並顯示 50 檔
    if (report.lowHeavyVol && report.lowHeavyVol.length > 0) {
      msg += "\n💥 <b>盤中預估爆量排行 (前50強)：</b>\n" + getTopList(report.lowHeavyVol, 50).join("\n");
    }
    // 2. 🔥 雙資齊買
    if (report.bothBuy && report.bothBuy.length > 0) {
      msg += "\n\n🔥 <b>雙資齊買 (外+投)：</b>\n" + getTopList(report.bothBuy, 10).join("\n");
    }
    // 3. 🚀 投信強勢認養
    if (report.trustBuy && report.trustBuy.length > 0) {
      msg += "\n\n🚀 <b>投信強勢認養：</b>\n" + getTopList(report.trustBuy, 10).join("\n");
    }
    // 4. 💎 籌碼高度集中
    if (report.highConc && report.highConc.length > 0) {
      msg += "\n\n💎 <b>籌碼高度集中 (>30%)：</b>\n" + getTopList(report.highConc, 10).join("\n");
    }
    // 5. 🚨 投信棄養警報
    if (report.trustSell && report.trustSell.length > 0) {
      msg += "\n\n🚨 <b>投信棄養警報：</b>\n" + getTopList(report.trustSell, 10, false).join("\n");
    }
  }

  // 發送訊息
  UrlFetchApp.fetch(`https://api.telegram.org/bot${CONFIG.TG_TOKEN}/sendMessage`, {
    "method": "post",
    "contentType": "application/json",
    "payload": JSON.stringify({ 
      "chat_id": CONFIG.TG_CHAT_ID, 
      "text": msg, 
      "parse_mode": "HTML", 
      "disable_web_page_preview": true 
    })
  });
}

function updateSheet(rows) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName("個股籌碼追蹤") || ss.insertSheet("個股籌碼追蹤");
  sheet.clear();
  sheet.getRange(1, 1, 1, 7).setValues([["代號","名稱","5日總變","總佔比","外資差","投信差","當日量比"]])
        .setBackground("#1B5E20").setFontColor("white").setFontWeight("bold");
  if (rows.length > 0) {
    rows.sort((a, b) => b[2] - a[2]); 
    sheet.getRange(2, 1, rows.length, 7).setValues(rows);
  }
}
