# Google Apps Script — Gmail → Bangla Summary → Telegram
# =========================================================
# Deploy at: https://script.google.com
# Trigger: Time-driven, every 1 minute (minimum polling)
#
# SETUP (3 steps):
# 1. Copy this whole file into a new Apps Script project
# 2. Run setupTriggers() ONCE to create the 1-min trigger
# 3. Set Script Properties (Project Settings → Script Properties):
#    - START_AT: 2026-10-15T15:47:00+06:00
#    - TELEGRAM_BOT_TOKEN: your_bot_token
#    - TELEGRAM_CHAT_ID: your_chat_id
#    - GEMINI_API_KEY: your_key   (optional but recommended)
#    - OPENROUTER_API_KEY: your_key
#    - GROQ_API_KEY: your_key
#
# For 3 Gmail accounts: deploy same script on each account,
# OR forward all mail to one account and deploy once.
# =========================================================

var SCRIPT_PROP = PropertiesService.getScriptProperties();
var SENT_IDS_KEY = 'SENT_IDS';
var TELEGRAM_LIMIT = 3900;

// ── Run ONCE to install 1-minute trigger ──
function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('processNewMail')
    .timeBased()
    .everyMinutes(1)
    .create();
  Logger.log('Trigger installed: every 1 minute');
}

// ── Main: called by trigger ──
function processNewMail() {
  var startAt = getStartAt();
  var sentIds = loadSentIds();
  var token = SCRIPT_PROP.getProperty('TELEGRAM_BOT_TOKEN');
  var chatId = SCRIPT_PROP.getProperty('TELEGRAM_CHAT_ID');
  if (!token || !chatId) {
    Logger.log('Missing Telegram config');
    return;
  }

  // Build Gmail search: unread + after START_AT date
  var dateStr = Utilities.formatDate(new Date(startAt * 1000),
    Session.getScriptTimeZone(), 'yyyy/MM/dd');
  var query = 'is:unread after:' + dateStr;
  
  var threads = GmailApp.search(query, 0, 5);
  if (threads.length === 0) return;

  var processed = 0;
  for (var t = 0; t < threads.length; t++) {
    var messages = threads[t].getMessages();
    for (var m = messages.length - 1; m >= 0; m--) {  // newest first
      var msg = messages[m];
      var msgId = msg.getId();

      // Skip already processed
      if (sentIds.indexOf(msgId) !== -1) continue;
      
      // Skip messages before START_AT
      if (msg.getDate().getTime() / 1000 < startAt) continue;

      // Skip already read (race condition guard)
      if (msg.isUnread() === false) continue;

      if (processed >= 2) return;  // max 2 per run to stay under 6 min
      processed++;

      var subject = msg.getSubject() || '(কোনো বিষয় নেই)';
      var sender = msg.getFrom() || '(অজানা প্রেরক)';
      var body = msg.getPlainBody();
      if (!body || body.length < 20) {
        body = stripHtml(msg.getBody() || '');
      }
      var account = Session.getActiveUser().getEmail();

      var result = summarize(subject, sender, account, body);
      if (!result) {
        Logger.log('SKIP: no valid summary for ' + msgId);
        continue;
      }

      var telegramText = buildTelegramMessage(account, sender, subject, result.summary, result.provider);
      if (telegramLength(telegramText) > TELEGRAM_LIMIT) {
        Logger.log('SKIP: message too long for ' + msgId);
        continue;
      }

      if (!sendToTelegram(token, chatId, telegramText)) {
        Logger.log('SKIP: Telegram send failed for ' + msgId);
        continue;
      }

      msg.markRead();
      sentIds.push(msgId);
      // Keep only last 500 IDs to stay under PropertiesService limit
      if (sentIds.length > 500) sentIds = sentIds.slice(-500);
      saveSentIds(sentIds);
      Logger.log('SENT: ' + msgId + ' via ' + result.provider);
    }
  }
}

// ── START_AT ──
function getStartAt() {
  var val = SCRIPT_PROP.getProperty('START_AT');
  if (val) {
    var dt = new Date(val);
    if (!isNaN(dt.getTime())) return dt.getTime() / 1000;
  }
  // Fallback: 24 hours ago
  return (Date.now() / 1000) - 86400;
}

// ── Duplicate tracking ──
function loadSentIds() {
  var raw = SCRIPT_PROP.getProperty(SENT_IDS_KEY);
  if (!raw) return [];
  try { return JSON.parse(raw); } catch(e) { return []; }
}

function saveSentIds(ids) {
  SCRIPT_PROP.setProperty(SENT_IDS_KEY, JSON.stringify(ids));
}

// ── HTML stripper ──
function stripHtml(html) {
  if (!html) return '';
  return html
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<(br|p|\/p|div|\/div|tr|\/tr|li|\/li|h[1-6])[^>]*>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, ' ')
    .trim();
}

// ── Telegram length check (UTF-16 LE) ──
function telegramLength(text) {
  return Utilities.newBlob(text, 'text/plain;charset=utf-8')
    .getBytes().length / 2;  // rough; accurate for BMP
}

// ── MarkdownV2 escape ──
function mdEscape(text) {
  if (!text) return '';
  return text.replace(/([_*\[\]()~`>#+\-=|{}.!])/g, '\\$1');
}

// ── Build LLM prompt ──
function buildPrompt(subject, sender, account, body) {
  var instructions = (
    'তুমি Gmail থেকে বাংলায় স্বাভাবিক সারাংশ, বিস্তারিত, নিরাপত্তা ও করণীয় অংশ চার ভাগে লিখবে। ' +
    'প্রথমে সারাংশ, তারপর বিস্তারিত, নিরাপত্তা ও করণীয়। ' +
    'কোনো HTML, CSS, Markdown, বা কোড ব্লক লিখবে না। ' +
    'বাংলা অক্ষর, বোল্ড ও ইমোজি ঠিক রাখবে; বিরাম চিহ্ন সঠিকভাবে বসাবে। ' +
    'স্পষ্ট, স্বাভাবিক বাংলায় লিখবে:\n' +
    'সারাংশ: এক-দুই বাক্যে মেইলের মূল বিষয়, প্রেরক, গুরুত্ব ও প্রয়োজনীয়তা জানাও।\n' +
    'বিস্তারিত: এক-দুই অনুচ্ছেদে মেইলের বিষয়বস্তু বিস্তারিত বলো এবং প্রয়োজনীয় তথ্য দাও।\n' +
    'নিরাপত্তা: ফিশিং, OTP, পেমেন্ট বা সন্দেহজনক লিংক থাকলে সতর্ক করো এবং ব্যাখ্যা দাও।\n' +
    'করণীয়: ব্যবহারকারীকে কী করতে হবে, উত্তর দিতে হবে নাকি অপেক্ষা করতে হবে তা পরামর্শ দাও।\n\n'
  );
  var data = JSON.stringify({
    gmail: account,
    sender: sender,
    subject: subject,
    email: (body || '').substring(0, 7000)
  });
  return instructions + data;
}

// ── Parse AI output ──
function parseAiOutput(text) {
  if (!text) return null;
  var sections = { summary: '', detail: '', security: '', action: '' };
  var labels = ['সারাংশ', 'বিস্তারিত', 'নিরাপত্তা', 'করণীয়'];
  var keys = ['summary', 'detail', 'security', 'action'];
  
  var pattern = new RegExp(
    '(?:^|\\n)\\s*(?:[-*#]+\\s*)?(' + labels.join('|') + ')\\s*[:：]\\s*', 'gm'
  );
  
  var matches = [];
  var match;
  while ((match = pattern.exec(text)) !== null) {
    matches.push({ index: match.index, label: match[1], end: pattern.lastIndex });
  }
  
  if (matches.length < 1) return null;
  
  for (var i = 0; i < matches.length; i++) {
    var keyIdx = labels.indexOf(matches[i].label);
    if (keyIdx === -1) continue;
    var startIdx = matches[i].end;
    var endIdx = (i + 1 < matches.length) ? matches[i + 1].index : text.length;
    sections[keys[keyIdx]] = text.substring(startIdx, endIdx).trim();
  }
  
  // Validate: must contain Bengali text
  var bengaliRe = /[\u0980-\u09FF]/;
  for (var k = 0; k < keys.length; k++) {
    var val = sections[keys[k]];
    if (val.length < 10 || !bengaliRe.test(val)) return null;
  }
  return sections;
}

// ── LLM call helpers ──
function httpPost(url, payload, headers) {
  var options = {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    headers: headers || {},
    muteHttpExceptions: true,
    escaping: false
  };
  try {
    var resp = UrlFetchApp.fetch(url, options);
    return JSON.parse(resp.getContentText());
  } catch (e) {
    return null;
  }
}

function callGemini(prompt) {
  var key = SCRIPT_PROP.getProperty('GEMINI_API_KEY');
  if (!key) return null;
  var url = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent';
  var res = httpPost(url, {
    contents: [{ parts: [{ text: prompt }] }],
    generationConfig: { temperature: 0.3, maxOutputTokens: 1200 }
  }, { 'x-goog-api-key': key });
  if (!res || !res.candidates) return null;
  var parts = res.candidates[0].content.parts || [];
  var text = parts.map(function(p) { return p.text || ''; }).join('').trim();
  return text || null;
}

function callOpenRouter(prompt) {
  var key = SCRIPT_PROP.getProperty('OPENROUTER_API_KEY');
  if (!key) return null;
  var res = httpPost('https://openrouter.ai/api/v1/chat/completions', {
    model: 'openrouter/free',
    messages: [{ role: 'user', content: prompt }],
    temperature: 0.3
  }, {
    Authorization: 'Bearer ' + key,
    'X-Title': 'Gmail to Telegram'
  });
  if (!res || !res.choices) return null;
  var text = (res.choices[0].message.content || '').trim();
  return text || null;
}

function callGroq(prompt) {
  var key = SCRIPT_PROP.getProperty('GROQ_API_KEY');
  if (!key) return null;
  var res = httpPost('https://api.groq.com/openai/v1/chat/completions', {
    model: 'openai/gpt-oss-20b',
    messages: [{ role: 'user', content: prompt }],
    temperature: 0.3
  }, {
    Authorization: 'Bearer ' + key
  });
  if (!res || !res.choices) return null;
  var text = (res.choices[0].message.content || '').trim();
  return text || null;
}

// ── Summarize: Gemini → OpenRouter → Groq ──
function summarize(subject, sender, account, body) {
  var prompt = buildPrompt(subject, sender, account, body);
  var providers = [
    { name: 'Gemini', fn: callGemini },
    { name: 'OpenRouter', fn: callOpenRouter },
    { name: 'Groq', fn: callGroq }
  ];
  
  for (var i = 0; i < providers.length; i++) {
    try {
      var text = providers[i].fn(prompt);
      if (!text) {
        Logger.log(providers[i].name + ': no text returned');
        continue;
      }
      var out = parseAiOutput(text);
      if (!out) {
        Logger.log(providers[i].name + ': parse failed');
        continue;
      }
      Logger.log('LLM_SUCCESS: ' + providers[i].name);
      return { summary: out, provider: providers[i].name };
    } catch (e) {
      Logger.log(providers[i].name + ' error: ' + e);
    }
  }
  return null;
}

// ── Build Telegram message ──
function buildTelegramMessage(account, sender, subject, out, provider) {
  return (
    '*📧 নতুন মেইল পাওয়া গেছে*\n\n' +
    '*📌 বিষয়:* ' + mdEscape(subject) + '\n' +
    '*📤 প্রেরক:* ' + mdEscape(sender) + '\n' +
    '*📥 ইমেইল Gmail\\-ই:* ' + mdEscape(account) + '\n' +
    '*🔖 মডেল:* ' + mdEscape(provider) + '\n\n' +
    '*📝 সারাংশ*\n' + mdEscape(out.summary) + '\n\n' +
    '*📋 বিস্তারিত*\n' + mdEscape(out.detail) + '\n\n' +
    '*🛡️ নিরাপত্তা*\n' + mdEscape(out.security) + '\n\n' +
    '*✅ করণীয়*\n' + mdEscape(out.action)
  );
}

// ── Send to Telegram ──
function sendToTelegram(token, chatId, text) {
  var url = 'https://api.telegram.org/bot' + token + '/sendMessage';
  var payload = {
    chat_id: chatId,
    text: text,
    parse_mode: 'MarkdownV2',
    disable_web_page_preview: true
  };
  var res = httpPost(url, payload, {});
  return res && res.ok === true;
}
