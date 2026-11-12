/**
 * CatDay — send Karen's purchase requests to the finance system.
 *
 * Paste this into Karen's form spreadsheet: Extensions → Apps Script, replace
 * everything, Save, then run  install  once (approve the permissions).
 * After that it sends every 5 minutes on its own, and there is a
 * "CatDay → Send requests to finance now" menu in the sheet.
 *
 * It only READS the sheet. Each complete request block (item + estimated total)
 * is sent; sending again just updates the same request in the system.
 */
var URL = '__URL__';
var KEY = '__KEY__';
var TAB = 'Request Forms';

function install() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'sendRequests') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('sendRequests').timeBased().everyMinutes(5).create();
  sendRequests();
  SpreadsheetApp.getActive().toast('Connected — requests now go to the finance system every 5 minutes.', 'CatDay', 8);
}

function onOpen() {
  SpreadsheetApp.getUi().createMenu('CatDay')
    .addItem('Send requests to finance now', 'sendNow')
    .addToUi();
}

function sendNow() {
  var r = sendRequests();
  SpreadsheetApp.getActive().toast(r, 'CatDay', 8);
}

function sendRequests() {
  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName(TAB) || ss.getSheets()[0];
  var range = sh.getDataRange();
  var v = range.getDisplayValues();
  var rich = range.getRichTextValues();
  var out = [], cur = null;

  function link(r, c) {           // a pasted link wins over the cell text
    try { var u = rich[r][c] && rich[r][c].getLinkUrl(); if (u) return u; } catch (e) {}
    return (v[r][c] || '').trim();
  }
  for (var r = 0; r < v.length; r++) {
    var a = (v[r][0] || '').trim();
    var m = a.match(/^REQUEST\s+(\d+)/i);
    if (m) { cur = { block: parseInt(m[1], 10) }; out.push(cur); continue; }
    if (!cur) continue;
    var b = (v[r][1] || '').trim(), d = (v[r][3] || '').trim();
    if (/^Request date/i.test(a)) { cur.request_date = b; cur.needed_by = d; }
    else if (/^Item/i.test(a)) cur.item = b;
    else if (/^Why/i.test(a)) cur.why = b;
    else if (/^Quantity/i.test(a)) { cur.qty = b; cur.total = d; }
    else if (/^Proposed payment/i.test(a)) { cur.pay_method = b; cur.supplier = d; }
    else if (/^Quote/i.test(a)) cur.quote = link(r, 1);
  }
  var filled = out.filter(function (x) { return x.item && x.total; });
  var res = UrlFetchApp.fetch(URL, {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    headers: { 'X-Intake-Key': KEY },
    payload: JSON.stringify({ sheet_id: ss.getId(), sheet_title: ss.getName(), requests: filled, complete: true })
  });
  var code = res.getResponseCode();
  if (code !== 200) return 'Finance system said ' + code + ' — tell Eugene.';
  var j = JSON.parse(res.getContentText());
  return 'Sent ' + filled.length + ' request(s): ' + j['new'].length + ' new, ' +
         j.updated.length + ' updated' + (j.flagged.length ? ', ' + j.flagged.length + ' changed after a decision (finance notified)' : '') + '.';
}
