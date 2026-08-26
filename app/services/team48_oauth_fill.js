(function () {
  var filledEmail = false;
  var filledPass = false;
  var pickedWorkspace = false;
  var lastClick = 0;

  function visible(el) {
    if (!el) return false;
    var st = window.getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || st.opacity === "0") return false;
    var box = el.getBoundingClientRect();
    return box.width > 8 && box.height > 8;
  }

  function setNative(el, value) {
    var proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    var desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
  }

  function firstMatch(selectors) {
    for (var i = 0; i < selectors.length; i++) {
      var nodes = document.querySelectorAll(selectors[i]);
      for (var j = 0; j < nodes.length; j++) {
        if (visible(nodes[j]) && !nodes[j].disabled) return nodes[j];
      }
    }
    return null;
  }

  function clickContinue() {
    var now = Date.now();
    if (now - lastClick < 1200) return;
    var nodes = document.querySelectorAll("button, [type=submit], [role=button]");
    var exact = null;
    var submit = null;
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (!visible(el) || el.disabled) continue;
      var text = String(el.innerText || el.value || "").replace(/\s+/g, " ").trim().toLowerCase();
      if (!text) continue;
      if (text.indexOf("google") >= 0 || text.indexOf("microsoft") >= 0 || text.indexOf("apple") >= 0 || text.indexOf("phone") >= 0) continue;
      if (text === "continue" || text === "next" || text === "continue with email" || text === "log in" || text === "login" || text === "继续" || text === "下一步" || text === "登录") {
        exact = el;
        break;
      }
      if (!submit && (el.type === "submit" || el.getAttribute("type") === "submit")) submit = el;
    }
    var btn = exact || submit;
    if (!btn) return;
    lastClick = now;
    btn.click();
  }

  function label(el) {
    return String(el.innerText || el.value || "").replace(/\s+/g, " ").trim();
  }

  function pickWorkspace() {
    if (pickedWorkspace) return false;
    var body = String((document.body && document.body.innerText) || "");
    var href = String(location.href || "").toLowerCase();
    if (href.indexOf("consent") < 0 && body.indexOf("工作空间") < 0 && body.toLowerCase().indexOf("workspace") < 0) return false;
    var want = String(window.TEAM48_TEAM || "").trim().toLowerCase();
    var nodes = document.querySelectorAll('button, [role="option"], [role="radio"], [role="listitem"], [role="button"]');
    var match = null;
    var teamLike = null;
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (!visible(el)) continue;
      var text = label(el);
      var low = text.toLowerCase();
      if (!text || text.length > 80 || text.indexOf("@") >= 0) continue;
      if (low === "cancel" || text === "取消" || low === "continue" || text === "继续" || text === "确认") continue;
      if (low.indexOf("personal") >= 0 || text.indexOf("个人") >= 0) continue;
      if (want && low.indexOf(want) >= 0) match = el;
      else if (!teamLike) teamLike = el;
    }
    var target = match || teamLike;
    if (target) target.click();
    var buttons = document.querySelectorAll("button");
    var confirm = null;
    for (var j = 0; j < buttons.length; j++) {
      var btn = buttons[j];
      if (!visible(btn) || btn.disabled) continue;
      var t = label(btn).toLowerCase();
      if (t === "cancel" || t === "取消") continue;
      if (t === "continue" || t === "确认" || t === "继续" || t === "allow" || t === "允许" || t === "") confirm = btn;
    }
    if (confirm) {
      pickedWorkspace = true;
      confirm.click();
      return true;
    }
    return false;
  }

  function tick() {
    if (pickWorkspace()) return;
    var email = String(window.TEAM48_EMAIL || "").trim();
    var password = String(window.TEAM48_PASSWORD || "");
    var emailEl = firstMatch([
      'input[type="email"]',
      'input[name="email"]',
      'input[name="username"]',
      'input[autocomplete="email"]',
      'input[autocomplete="username"]',
      'input[id*="email" i]',
      'input[id*="username" i]'
    ]);
    if (email && emailEl && !filledEmail) {
      if (emailEl.value !== email) setNative(emailEl, email);
      filledEmail = emailEl.value === email;
      if (filledEmail) clickContinue();
      return;
    }
    var passEl = firstMatch(['input[type="password"]', 'input[name="password"]', 'input[autocomplete="current-password"]']);
    if (password && passEl && !filledPass) {
      if (passEl.value !== password) setNative(passEl, password);
      filledPass = passEl.value === password;
      if (filledPass) clickContinue();
    }
  }

  tick();
  setInterval(tick, 500);
  var obs = new MutationObserver(function () { tick(); });
  obs.observe(document.documentElement || document.body, { childList: true, subtree: true });
})();
