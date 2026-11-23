/* TimeFence UI. No inline handlers, no external resources. */
(function () {
  "use strict";

  var body = document.body;
  var base = body.dataset.base || "";
  var csrf = body.dataset.csrf || "";
  var page = body.dataset.page;

  var i18n = {};
  var state = null;
  var role = "";
  var tab = "today";
  var timezone = "UTC";
  var servicesCache = null;
  var discoverData = null;
  var alertsConfig = null;
  var alertsTabLoaded = false;
  var alertsHintLoaded = false;
  /* A dismissed reminder comes back after two weeks, while alerts stay off. */
  var HINT_HIDDEN_KEY = "tf.alertsHintHiddenAt";
  var HINT_HIDDEN_MS = 14 * 24 * 60 * 60 * 1000;
  var hintHiddenFor = 0;  /* the same dismissal, kept for a view without storage */
  var usageDays = 7;
  var usageData = null;
  var usageLoading = false;
  var sequence = 0;
  var applied = 0;
  var renderedSignature = null;
  var modalOpener = null;
  var shownTab = "";
  var inflight = {};
  var busy = {};
  var freeTimeChoice = {};
  /* Whether a group's Custom minutes box is open: the poll rebuilds the
     controls, so the disclosure state cannot live in the markup. */
  var openCustom = {};
  /* A Free tap that has not been confirmed with a time yet. The poll rebuilds
     the controls, so an open panel has to live outside the markup too. */
  var pendingFree = {};

  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* A function replacer keeps "$&" and friends in names literal. */
  function t(key, vars) {
    var template = i18n[key] || key;
    return String(template).replace(/\{(\w+)\}/g, function (whole, name) {
      if (vars && Object.prototype.hasOwnProperty.call(vars, name)) { return String(vars[name]); }
      return whole;
    });
  }

  function clock(ts) {
    var seconds = Number(ts);
    if (!seconds) { return ""; }
    try {
      return new Intl.DateTimeFormat(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        timeZone: timezone
      }).format(new Date(seconds * 1000));
    } catch (err) {
      return "";
    }
  }

  function fmtMinutes(value) {
    var n = Math.round(Number(value) * 10) / 10;
    return (n % 1 === 0 ? String(n) : n.toFixed(1));
  }

  /* Minutes since midnight for an HH:MM value, or null when it is not one. */
  function minutesOfDay(value) {
    var match = /^(\d{1,2}):(\d{2})$/.exec(String(value || ""));
    if (!match) { return null; }
    return (Number(match[1]) % 24) * 60 + Number(match[2]);
  }

  function pad2(value) {
    return (value < 10 ? "0" : "") + value;
  }

  /* The server's clock, not the browser's: a free window ends at the server's
     local midnight, so both sides must agree on what "today" means. */
  function serverNowMinutes() {
    if (!state || !state.app || !state.app.now) { return null; }
    try {
      var text = new Intl.DateTimeFormat("en-GB", {
        timeZone: state.app.timezone || timezone,
        hourCycle: "h23",
        hour: "2-digit",
        minute: "2-digit"
      }).format(new Date(Number(state.app.now) * 1000));
      return minutesOfDay(text);
    } catch (err) {
      return null;
    }
  }

  /* An hour ahead, rounded up to the next quarter hour, in the server zone. */
  function defaultFreeTime() {
    var minutes = serverNowMinutes();
    if (minutes === null) { return ""; }
    var ahead = (Math.ceil((minutes + 60) / 15) * 15) % 1440;
    return pad2(Math.floor(ahead / 60)) + ":" + pad2(ahead % 60);
  }

  /* The server rolls a time that is not later than now to tomorrow. */
  function freeTimeIsTomorrow(value) {
    var chosen = minutesOfDay(value);
    var minutes = serverNowMinutes();
    if (chosen === null || minutes === null) { return false; }
    return chosen <= minutes;
  }

  /* HH:MM in the server zone, whatever the browser's own clock looks like: a
     time input only ever accepts 24-hour text. */
  function hhmm(ts) {
    var seconds = Number(ts);
    if (!seconds) { return ""; }
    try {
      return new Intl.DateTimeFormat("en-GB", {
        timeZone: timezone,
        hourCycle: "h23",
        hour: "2-digit",
        minute: "2-digit"
      }).format(new Date(seconds * 1000));
    } catch (err) {
      return "";
    }
  }

  /* A time the admin typed wins over the default for the life of the page. A
     group that is already free starts at the end of its window, so Allow
     changes that window instead of offering a fresh default. */
  function freeTimeValue(group) {
    var id = String(group.id);
    if (freeTimeChoice[id]) { return freeTimeChoice[id]; }
    if (group.status === "free" && group.free_until) {
      return hhmm(group.free_until) || defaultFreeTime();
    }
    return defaultFreeTime();
  }

  /* The default drifts with the server clock, so an input nobody has touched is
     re-pointed on every render: the controls markup stays, and a focused input
     is left alone. An edited value wins for the life of the page. */
  function refreshFreeDefault(card, group) {
    var id = String(group.id);
    if (freeTimeChoice[id]) { return; }
    var input = card.querySelector("[data-free-time]");
    if (!input || document.activeElement === input) { return; }
    var value = freeTimeValue(group);
    if (!value) { return; }
    if (input.value !== value) { input.value = value; }
    var hint = card.querySelector("[data-free-tomorrow]");
    if (hint) { hint.classList.toggle("hidden", !freeTimeIsTomorrow(value)); }
  }

  /* One displayed mode per group, straight from the server payload: a paused
     card shows no segments, a free window wins over everything and every other
     status (bedtime and a used-up budget included) follows the daily budget. */
  function modeFor(group) {
    if (!group || group.paused) { return ""; }
    if (group.status === "free") { return "free"; }
    if (group.status === "blocked_admin") { return "block"; }
    return "auto";
  }

  /* The calls a move from one mode to another needs, in order, as route
     suffixes. Free and a stored block can coexist, so a few moves are two
     calls; the order is chosen so a failed second call still leaves the screen
     telling the truth about the first one. */
  function modeSequence(from, to, group) {
    var stored = Boolean(group && Number(group.force_block_until) > 0);
    if (to === "block") { return from === "block" ? [] : ["block"]; }
    if (to === "auto") {
      if (from === "free") { return stored ? ["force/clear", "free/clear"] : ["free/clear"]; }
      return from === "block" ? ["force/clear"] : [];
    }
    if (to === "free") { return from === "block" ? ["free", "force/clear"] : ["free"]; }
    return [];
  }

  function modeMessage(from, to, name, until) {
    if (to === "block") { return t("toast.blocked", { name: name }); }
    if (to === "free") { return t("toast.free", { name: name, time: until }); }
    if (from === "free") { return t("toast.free_cleared", { name: name }); }
    return t("toast.unblocked", { name: name });
  }

  function toast(message) {
    var el = $("#toast");
    if (!el || !message) { return; }
    el.textContent = message;
    el.hidden = false;
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(function () { el.hidden = true; }, 3200);
  }

  function alertBox() {
    var el = $("#global-alert");
    if (!el || !state) { return; }
    var lines = [];
    if (state.adguard_error) {
      lines.push(state.adguard_error_kind === "auth"
        ? t("error.adguard_auth", { seconds: state.adguard_retry_seconds || 0 })
        : t("error.adguard", { detail: state.adguard_error }));
    }
    (state.warnings || []).forEach(function (code) {
      var text = t("warn." + code);
      if (text && text !== "warn." + code) { lines.push(text); }
    });
    if (!lines.length) {
      el.textContent = "";
      el.classList.add("hidden");
      return;
    }
    el.textContent = lines.join(" ");
    el.classList.remove("hidden");
  }

  function api(method, path, payload) {
    return fetch(base + path, {
      method: method,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: payload === undefined ? undefined : JSON.stringify(payload)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          var error = new Error(data.error || ("HTTP " + response.status));
          error.payload = data;
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }

  function setState(next) {
    if (next && next.app) {
      state = next;
      role = next.app.role || role;
      csrf = next.app.csrf || csrf;
      timezone = next.app.timezone || timezone;
    }
  }

  function openModal(html) {
    var modal = $("#modal");
    if (modal.classList.contains("hidden")) { modalOpener = document.activeElement; }
    $("#modal-sheet").innerHTML = html;
    var heading = $("#modal-sheet").querySelector("h2");
    if (heading) {
      heading.id = "modal-title";
      modal.setAttribute("aria-labelledby", heading.id);
    }
    modal.classList.remove("hidden");
    var focusable = $("#modal-sheet").querySelector("input, textarea, button, select");
    if (focusable) { focusable.focus(); }
  }

  function closeModal() {
    var modal = $("#modal");
    modal.classList.add("hidden");
    modal.removeAttribute("aria-labelledby");
    $("#modal-sheet").innerHTML = "";
    var opener = modalOpener;
    modalOpener = null;
    /* Focus goes back to what opened the dialog, not to the top of the page. */
    if (opener && opener.focus && document.contains(opener)) { opener.focus(); }
  }

  function modalFocusable() {
    return $$(
      "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), " +
        "textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
      $("#modal-sheet")
    ).filter(function (el) { return el.getClientRects().length > 0; });
  }

  /* Identity of the focused control, so a re-render can put focus back. */
  function focusKey(element) {
    if (!element || element === document.body || !element.getAttribute) { return ""; }
    var names = [
      "id",
      "data-custom-bonus",
      "data-free-time",
      "data-group",
      "data-action",
      "data-minutes",
      "data-user",
      "data-role"
    ];
    var selector = "";
    names.forEach(function (name) {
      if (element.getAttribute(name) !== null) {
        selector += "[" + name + '="' + String(element.getAttribute(name)).replace(/"/g, '\\"') + '"]';
      }
    });
    return selector;
  }

  function captureFocus() {
    var element = document.activeElement;
    var selector = focusKey(element);
    if (!selector) { return null; }
    var text = "value" in element ? element.value : null;
    var start = null;
    var end = null;
    if (element.selectionStart != null) {
      start = element.selectionStart;
      end = element.selectionEnd;
    }
    return { selector: selector, value: text, start: start, end: end };
  }

  function restoreFocus(saved) {
    if (!saved) { return; }
    var target = document.querySelector(saved.selector);
    if (!target) { return; }
    if (saved.value !== null && "value" in target && !target.value) { target.value = saved.value; }
    if (document.activeElement !== target) { target.focus(); }
    if (saved.start != null && target.setSelectionRange) {
      try { target.setSelectionRange(saved.start, saved.end); } catch (err) { /* not a text field */ }
    }
  }

  function findGroup(groupId) {
    if (!state || !state.groups) { return null; }
    return state.groups.filter(function (g) { return g.id === groupId; })[0] || null;
  }

  // ------------------------------------------------------------------ render
  /* One status per group, straight from the server: no clock maths here. */
  function statusFor(group) {
    if (group.status === "bedtime") {
      var until = group.bedtime_until;
      return {
        cls: "warn",
        text: until ? t("status.bedtime", { time: until }) : t("status.bedtime_any")
      };
    }
    if (group.status === "paused") { return { cls: "warn", text: t("status.paused") }; }
    if (group.status === "blocked_admin") { return { cls: "bad", text: t("status.forced") }; }
    if (group.status === "free") {
      return { cls: "warn", text: t("status.free", { time: clock(group.free_until) }) };
    }
    if (group.status === "limit_reached") { return { cls: "bad", text: t("status.limit_reached") }; }
    return {
      cls: "ok",
      text: t("status.allowed_auto", { minutes: fmtMinutes(group.remaining_minutes) })
    };
  }

  function coverage(group) {
    var parts = [];
    if (group.services && group.services.length) { parts.push(group.services.join(", ")); }
    if (group.domains && group.domains.length) { parts.push(group.domains.join(", ")); }
    return parts.join(" · ");
  }

  function liveHtml(group) {
    var total = Math.max(0, (Number(group.minutes_today) || 0) + (Number(group.bonus_minutes) || 0));
    var used = Number(group.used_minutes) || 0;
    var usedPercent = total > 0 ? Math.min(100, (used / total) * 100) : 0;
    var leftover = Math.max(0, total - used);
    var usedText = esc(t("budget.used", { used: fmtMinutes(used) }));
    return '<div class="between mt"><div class="big">' + fmtMinutes(group.remaining_minutes) +
      '<span class="muted small"> ' + esc(t("common.minutes")) + "</span></div>" +
      '<div class="muted small center">' +
      esc(t("budget.left", { left: fmtMinutes(leftover), total: fmtMinutes(total) })) +
      "<br>" + usedText + "</div></div>" +
      '<progress max="100" value="' + usedPercent.toFixed(1) + '" aria-label="' + usedText + '"></progress>';
  }

  /* A card is three regions: a header that is patched in place, a live part that
     is redrawn when its markup changes, and controls that belong to a signature.
     Anything that has to survive a poll lives in the header or in a JS map. */
  function buildHeader() {
    var header = document.createElement("header");
    header.dataset.header = "1";
    var title = document.createElement("h2");
    var emoji = document.createElement("span");
    emoji.dataset.headerEmoji = "1";
    emoji.setAttribute("aria-hidden", "true");
    var name = document.createElement("span");
    name.dataset.headerName = "1";
    var status = document.createElement("span");
    status.dataset.headerStatus = "1";
    title.appendChild(emoji);
    title.appendChild(document.createTextNode(" "));
    title.appendChild(name);
    title.appendChild(document.createTextNode(" "));
    title.appendChild(status);
    var line = document.createElement("div");
    line.className = "sub wrap-anywhere";
    line.dataset.headerCoverage = "1";
    header.appendChild(title);
    header.appendChild(line);
    return header;
  }

  /* Header updates touch textContent and className only: no node is replaced,
     so a control placed here later survives the five-second poll. */
  function paintHeader(card, group) {
    var header = card.querySelector("[data-header]");
    if (!header) { return; }
    var status = statusFor(group);
    header.querySelector("[data-header-emoji]").textContent = group.emoji || "⏳";
    header.querySelector("[data-header-name]").textContent = group.name;
    header.querySelector("[data-header-coverage]").textContent = coverage(group);
    var pill = header.querySelector("[data-header-status]");
    pill.textContent = status.text;
    pill.className = "pill " + status.cls;
    pill.dataset.status = String(group.id);
  }

  /* One segment of the mode control: a plain button with aria-pressed, never a
     radio, because an arrow key must move focus without applying anything. */
  function modeButton(group, mode, pressed) {
    var text = mode === "block"
      ? t("admin.mode_block")
      : mode === "free"
        ? t("admin.mode_free")
        : t("admin.mode_auto");
    return '<button type="button" class="btn seg-btn" data-action="mode" data-mode="' + mode +
      '" data-group="' + group.id + '" aria-pressed="' + (pressed ? "true" : "false") + '">' +
      esc(text) + "</button>";
  }

  function controlsHtml(group) {
    var id = String(group.id);
    if (group.paused) {
      return '<div class="row tight mt resume-row">' +
        '<button type="button" class="btn primary" data-action="resume" data-group="' + group.id +
        '" aria-label="' + esc(t("admin.resume_for", { name: group.name })) + '">' +
        esc(t("admin.resume")) + "</button></div>";
    }
    var mode = modeFor(group);
    var free = mode === "free";
    var storedBlock = Number(group.force_block_until) > 0;
    var until = freeTimeValue(group);
    var tomorrow = freeTimeIsTomorrow(until);
    var open = free || Boolean(pendingFree[id]);
    var panelId = "custom-bonus-" + group.id;
    return '<div class="seg" role="group" aria-label="' +
      esc(t("admin.mode_label", { name: group.name })) + '" data-mode-group="' + group.id + '">' +
      modeButton(group, "block", mode === "block") +
      modeButton(group, "auto", mode === "auto") +
      modeButton(group, "free", free) +
      "</div>" +
      '<div class="row tight mt free-panel' + (open ? "" : " hidden") +
      '" data-free-panel="' + group.id + '">' +
      '<input type="time" data-free-time="' + group.id + '" value="' + esc(until) +
      '" aria-label="' + esc(t("admin.free_label")) + '">' +
      '<span class="muted small' + (tomorrow ? "" : " hidden") +
      '" data-free-tomorrow="' + group.id + '">' + esc(t("admin.free_tomorrow")) + "</span>" +
      '<button type="button" class="btn primary" data-action="free-apply" data-group="' +
      group.id + '" aria-label="' +
      esc(t("admin.free_allow_for", { name: group.name })) + '">' +
      esc(t("admin.free_allow")) + "</button>" +
      '<button type="button" class="btn' + (free ? " hidden" : "") +
      '" data-action="free-cancel" data-free-cancel="' + group.id + '" data-group="' + group.id +
      '">' + esc(t("common.cancel")) + "</button>" +
      "</div>" +
      '<p class="muted small free-note' + (free && storedBlock ? "" : " hidden") +
      '" data-free-note="' + group.id + '">' + esc(t("admin.free_then_note")) + "</p>" +
      '<div class="row tight mt bonus-row">' +
      '<button class="btn" data-action="bonus" data-group="' + group.id + '" data-minutes="-15"' +
      ' aria-label="' + esc(t("admin.decrease_for", { name: group.name })) + '">' +
      esc(t("admin.decrease")) + "</button>" +
      '<button class="btn" data-action="bonus" data-group="' + group.id + '" data-minutes="15"' +
      ' aria-label="' + esc(t("admin.bonus_for", { name: group.name })) + '">' +
      esc(t("admin.bonus")) + "</button>" +
      '<button class="btn custom-toggle" data-action="custom" data-group="' + group.id +
      '" aria-expanded="false" aria-controls="' + panelId + '" aria-label="' +
      esc(t("admin.custom_for", { name: group.name })) + '">' +
      esc(t("admin.custom")) + '<span class="chev" aria-hidden="true">▾</span></button>' +
      "</div>" +
      '<div class="row tight mt bonus-panel hidden" id="' + panelId +
      '" data-custom-panel="' + group.id + '">' +
      '<input type="number" inputmode="numeric" step="5" data-custom-bonus="' + group.id +
      '" placeholder="' + esc(t("admin.custom_placeholder")) + '" aria-label="' +
      esc(t("admin.custom_placeholder")) + '">' +
      '<button class="btn primary" data-action="bonus-custom" data-group="' + group.id +
      '" aria-label="' + esc(t("admin.apply_for", { name: group.name })) + '">' +
      esc(t("admin.apply")) + "</button>" +
      "</div>" +
      '<button type="button" class="btn ghost" data-action="reset" data-group="' + group.id + '">' +
      esc(t("admin.reset_today")) + "</button>" +
      "</div>";
  }

  /* The disclosure's open state is painted from the map, so a rebuilt controls
     region comes back open with whatever was typed in it. */
  function paintCustom(card, groupId) {
    if (!card) { return; }
    var id = String(groupId);
    var open = Boolean(openCustom[id]);
    var panel = card.querySelector('[data-custom-panel="' + id + '"]');
    var toggle = card.querySelector('[data-action="custom"][data-group="' + id + '"]');
    if (panel) { panel.classList.toggle("hidden", !open); }
    if (toggle) { toggle.setAttribute("aria-expanded", open ? "true" : "false"); }
  }

  /* Server truth, patched in place on every poll: the pressed segment, the Free
     panel and its note. No node is replaced, so focus stays on a segment while
     a free window or a block expires. Only an open pending panel is local. */
  function paintMode(card, group) {
    if (!card || !group) { return; }
    var id = String(group.id);
    var mode = modeFor(group);
    var storedBlock = Number(group.force_block_until) > 0;
    $$('[data-action="mode"]', card).forEach(function (button) {
      button.setAttribute("aria-pressed", button.dataset.mode === mode ? "true" : "false");
    });
    var open = mode === "free" || Boolean(pendingFree[id]);
    var panel = card.querySelector('[data-free-panel="' + id + '"]');
    if (panel) { panel.classList.toggle("hidden", !open); }
    var cancel = card.querySelector('[data-free-cancel="' + id + '"]');
    if (cancel) { cancel.classList.toggle("hidden", mode === "free"); }
    var note = card.querySelector('[data-free-note="' + id + '"]');
    if (note) { note.classList.toggle("hidden", !(mode === "free" && storedBlock)); }
  }

  function signatureFor(group) {
    return [
      group.paused ? "paused" : "",
      modeFor(group),
      group.free_until ? String(group.free_until) : "",
      Number(group.force_block_until) > 0 ? "stored" : ""
    ].join("|");
  }

  /* Patch the cards: only the live part is redrawn on every poll, the controls
     only when their state changed and nobody is typing in them. */
  function renderToday() {
    var container = $("#groups");
    if (!container || !state) { return; }
    var groups = state.groups || [];
    var wanted = {};

    groups.forEach(function (group) { wanted[String(group.id)] = true; });
    $$("[data-card-group]", container).forEach(function (card) {
      if (!wanted[card.dataset.cardGroup]) { container.removeChild(card); }
    });

    groups.forEach(function (group, index) {
      var id = String(group.id);
      var card = container.querySelector('[data-card-group="' + id + '"]');
      if (!card) {
        card = document.createElement("article");
        card.className = "card";
        card.dataset.cardGroup = id;
        card.appendChild(buildHeader());
        var live = document.createElement("div");
        live.dataset.live = "1";
        var controls = document.createElement("div");
        controls.dataset.controls = "1";
        card.appendChild(live);
        card.appendChild(controls);
      }
      /* Only move the card when the order really changed: re-appending a card
         that holds the focused input blurs it in every browser. */
      if (card.parentNode !== container) {
        container.appendChild(card);
      } else {
        var previous = index > 0
          ? container.querySelector('[data-card-group="' + String(groups[index - 1].id) + '"]')
          : null;
        if (card.previousElementSibling !== previous) {
          container.insertBefore(card, previous ? previous.nextSibling : container.firstChild);
        }
      }

      paintHeader(card, group);

      var live = card.querySelector("[data-live]");
      var liveMarkup = liveHtml(group);
      if (live.dataset.html !== liveMarkup) {
        live.innerHTML = liveMarkup;
        live.dataset.html = liveMarkup;
      }

      var controls = card.querySelector("[data-controls]");
      var input = controls.querySelector("[data-custom-bonus]");
      var typing = controls.contains(document.activeElement) || (input && input.value !== "");
      var signature = signatureFor(group);
      if (card.dataset.signature !== signature && !typing) {
        controls.innerHTML = controlsHtml(group);
        card.dataset.signature = signature;
      }

      paintCustom(card, group.id);
      refreshFreeDefault(card, group);
    });

    var empty = $("#no-groups");
    if (empty) { empty.classList.toggle("hidden", groups.length > 0); }
  }

  function renderGroupList() {
    var list = $("#group-list");
    if (!list || !state) { return; }
    if (!state.groups.length) {
      list.innerHTML = '<p class="muted small">' + esc(t("groups.empty")) + "</p>";
      return;
    }
    list.innerHTML = state.groups.map(function (g) {
      return (
        '<div class="item">' +
        '<span aria-hidden="true">' + esc(g.emoji || "⏳") + "</span>" +
        '<div class="grow"><div class="name">' + esc(g.name) + "</div>" +
        '<div class="muted small">' + g.weekday_minutes + "/" + g.weekend_minutes + " " +
        esc(t("common.minutes")) + " · " + esc(g.owner || t("groups.owner_shared")) +
        " · " + esc([].concat(g.services, g.domains).join(", ").slice(0, 70)) + "</div></div>" +
        (g.paused ? '<span class="pill warn">' + esc(t("status.paused")) + "</span>" : "") +
        '<button class="btn ghost" data-action="edit" data-group="' + g.id + '">' +
        esc(t("common.edit")) + "</button>" +
        '<button class="btn ghost" data-action="pause" data-group="' + g.id + '" data-paused="' +
        (g.paused ? "0" : "1") + '">' + (g.paused ? "▶" : "⏸") + "</button>" +
        "</div>"
      );
    }).join("");
  }

  function usageGroups() {
    var seen = {};
    var out = [];
    ((usageData && usageData.rows) || []).forEach(function (row) {
      var key = String(row.group_id);
      if (seen[key]) { return; }
      seen[key] = true;
      out.push({ id: row.group_id, name: row.group });
    });
    return out;
  }

  function renderHistory() {
    var table = $("#history-table");
    if (!table) { return; }
    var select = $("#history-days");
    if (select) { select.value = String(usageDays); }
    var link = $("#history-csv");
    if (link) { link.href = base + "/api/usage.csv?days=" + usageDays; }
    if (!usageData) {
      table.innerHTML = '<tbody><tr><td class="muted small">' + esc(t("common.loading")) +
        "</td></tr></tbody>";
      return;
    }
    var groups = usageGroups();
    if (!groups.length) {
      table.innerHTML = '<tbody><tr><td class="muted small">' + esc(t("history.none")) +
        "</td></tr></tbody>";
      return;
    }
    var order = [];
    var byDay = {};
    usageData.rows.forEach(function (row) {
      if (!byDay[row.day]) { byDay[row.day] = {}; order.push(row.day); }
      byDay[row.day][String(row.group_id)] = row;
    });
    var head = '<thead><tr><th>' + esc(t("history.day")) + "</th>" +
      groups.map(function (group) {
        return "<th>" + esc(group.name) + "</th>";
      }).join("") + "</tr></thead>";
    var rows = order.map(function (day) {
      return "<tr><td>" + esc(day) + "</td>" + groups.map(function (group) {
        var row = byDay[day][String(group.id)];
        var value = row ? Number(row.used_minutes) || 0 : 0;
        return "<td>" + (value ? fmtMinutes(value) : "–") + "</td>";
      }).join("") + "</tr>";
    }).join("");
    table.innerHTML = head + "<tbody>" + rows + "</tbody>";
  }

  function loadUsage() {
    if (usageLoading) { return Promise.resolve(); }
    usageLoading = true;
    return api("GET", "/api/usage?days=" + usageDays).then(function (data) {
      usageData = data || { days: usageDays, rows: [] };
      renderHistory();
    }).catch(function (error) {
      toast(error.message);
    }).then(function () { usageLoading = false; });
  }

  /* Everything the DOM shows, minus the ever-changing app.now: a poll that
     changes nothing must not touch the DOM at all. */
  function viewSignature() {
    if (!state) { return ""; }
    var app = state.app || {};
    return JSON.stringify({
      role: app.role || "",
      username: app.username || "",
      tab: tab,
      warnings: state.warnings || [],
      adguard_error: state.adguard_error || "",
      adguard_error_kind: state.adguard_error_kind || "",
      adguard_retry_seconds: state.adguard_retry_seconds || 0,
      groups: state.groups || [],
      usage: usageData ? { days: usageData.days, rows: usageData.rows } : null,
      usageDays: usageDays
    });
  }

  /* The strip scrolls sideways on a phone, so the selected tab is brought back
     into view whenever the shown tab changes (never on every poll). */
  function revealSelectedTab() {
    if (tab === shownTab) { return; }
    shownTab = tab;
    var button = $('#admin-tabs .tab[aria-selected="true"]');
    if (button && button.scrollIntoView) {
      button.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  function render() {
    if (!state) { return; }
    var signature = viewSignature();
    if (signature === renderedSignature) { return; }
    renderedSignature = signature;
    var focus = captureFocus();
    var isAdmin = role === "admin";
    $("#role-badge").textContent = isAdmin ? "admin" : "user";
    $("#admin-tabs").classList.toggle("hidden", !isAdmin);
    $$("#admin-tabs .tab").forEach(function (button) {
      button.setAttribute("aria-selected", button.dataset.tab === tab ? "true" : "false");
    });
    revealSelectedTab();
    if (!isAdmin) { tab = "today"; }
    $("#panel-groups").classList.toggle("hidden", !(isAdmin && tab === "groups"));
    $("#panel-history").classList.toggle("hidden", !(isAdmin && tab === "history"));
    $("#panel-settings").classList.toggle("hidden", !(isAdmin && tab === "settings"));
    $("#panel-today").classList.toggle("hidden", isAdmin && tab !== "today");
    renderToday();
    if (isAdmin && tab === "groups") { renderGroupList(); }
    if (isAdmin && tab === "history") {
      renderHistory();
      if (!usageData) { loadUsage(); }
    }
    if (isAdmin && tab === "settings") {
      loadUsers();
      if (!alertsTabLoaded) { alertsTabLoaded = true; loadAlerts(); }
    }
    maybeLoadAlertsHint();
    alertBox();
    restoreFocus(focus);
  }

  // ---------------------------------------------------------------- refresh
  function refresh() {
    var mine = sequence + 1;
    sequence = mine;
    return api("GET", "/api/state").then(function (data) {
      if (mine < applied) { return; }  /* a late, older response is dropped */
      applied = mine;
      setState(data);
      render();
    }).catch(function (error) {
      if (error.status === 401) { window.location.href = base + "/login"; }
    });
  }

  // ---------------------------------------------------------------- actions
  /* A busy card refuses another action, but it never disables its buttons: a
     focused element that becomes disabled loses focus to <body>, and the next
     poll cannot put it back. aria-disabled is inert, so focus stays put. */
  function setBusy(groupId, delta) {
    busy[groupId] = Math.max(0, (busy[groupId] || 0) + delta);
    var card = document.querySelector('[data-card-group="' + groupId + '"]');
    if (!card) { return; }
    var isBusy = busy[groupId] > 0;
    card.setAttribute("aria-busy", isBusy ? "true" : "false");
    $$('button[data-group="' + groupId + '"]', card).forEach(function (button) {
      if (isBusy) {
        button.setAttribute("aria-disabled", "true");
      } else {
        button.removeAttribute("aria-disabled");
      }
    });
  }

  /* One action per button at a time: a double tap must not apply twice.
     `done` runs only when the server accepted the call. */
  function adminAction(action, groupId, extra, message, done) {
    var key = action + ":" + groupId;
    if (inflight[key]) { return; }
    inflight[key] = true;
    setBusy(groupId, 1);
    var payload = { group_id: groupId };
    if (extra) {
      Object.keys(extra).forEach(function (name) { payload[name] = extra[name]; });
    }
    api("POST", "/api/groups/" + groupId + "/" + action, payload)
      .then(function (data) {
        if (done) { done(data); }
        return refresh();
      })
      .then(function () { if (message) { toast(message); } })
      .catch(function (error) { toast(error.message); })
      .then(function () {
        delete inflight[key];
        setBusy(groupId, -1);
      });
  }

  /* Every mode change goes through here: sequential calls under one in-flight
     key, the card stays busy across all of them, one refresh and one toast. */
  function setMode(groupId, to, until) {
    var key = "mode:" + groupId;
    if (inflight[key]) { return; }
    var group = findGroup(groupId);
    if (!group) { return; }
    var from = modeFor(group);
    var steps = modeSequence(from, to, group);
    if (!steps.length) { return; }  /* the pressed segment: nothing to do */
    inflight[key] = true;
    setBusy(groupId, 1);
    var failed = false;
    var chain = Promise.resolve();
    steps.forEach(function (step) {
      chain = chain.then(function () {
        if (failed) { return null; }
        var payload = step === "free" ? { until: until } : {};
        return api("POST", "/api/groups/" + groupId + "/" + step, payload).catch(function (error) {
          failed = true;
          toast(error.message);
          return null;
        });
      });
    });
    return chain.then(function () {
      if (!failed) {
        delete freeTimeChoice[String(groupId)];
        pendingFree[String(groupId)] = false;
      }
      /* Even a failed second call refreshes: the screen shows the server. */
      return refresh().then(function () {
        if (!failed) { toast(modeMessage(from, to, group.name, until)); }
      });
    }).then(function () {
      delete inflight[key];
      setBusy(groupId, -1);
    });
  }

  function bonusMessage(name, minutes) {
    return minutes < 0
      ? t("toast.bonus_removed", { name: name, minutes: Math.abs(minutes) })
      : t("toast.bonus", { name: name, minutes: minutes });
  }

  function resetGroup(groupId, name) {
    openModal(
      '<h2 class="wrap-anywhere">' + esc(t("admin.reset_today")) + " — " + esc(name) + "</h2>" +
      '<p class="sub">' + esc(t("admin.reset_confirm", { name: name })) + "</p>" +
      '<div class="row mt"><button class="btn" data-action="modal-close">' +
      esc(t("common.cancel")) + "</button>" +
      '<button class="btn danger" id="reset-confirm">' + esc(t("admin.reset_today")) + "</button></div>"
    );
    $("#reset-confirm").addEventListener("click", function (event) {
      var button = event.currentTarget;
      button.disabled = true;
      api("POST", "/api/groups/" + groupId + "/reset", {})
        .then(function (data) {
          closeModal();
          toast(t("toast.reset", { name: data.group.name }));
          return refresh();
        })
        .catch(function (error) {
          button.disabled = false;
          toast(error.message);
        });
    });
  }

  // ------------------------------------------------------------ group editor
  function criticalDialog(hits, retry) {
    var list = hits.map(function (h) {
      return '<li><strong>' + esc(h.domain) + "</strong> — " + esc(h.note) + "</li>";
    }).join("");
    openModal(
      '<h2 class="wrap-anywhere">' + esc(t("groups.critical_title")) + "</h2>" +
      '<ul class="small mt">' + list + "</ul>" +
      '<div class="row mt"><button class="btn" data-action="modal-close">' +
      esc(t("common.cancel")) + "</button>" +
      '<button class="btn danger" id="critical-confirm">' +
      esc(t("groups.critical_confirm")) + "</button></div>"
    );
    $("#critical-confirm").addEventListener("click", function () {
      closeModal();
      retry(true);
    });
  }

  function saveGroup(payload, groupId, confirmCritical) {
    var method = groupId ? "PUT" : "POST";
    var path = groupId ? "/api/groups/" + groupId : "/api/groups";
    if (confirmCritical) { payload.confirm_critical = true; }
    api(method, path, payload).then(function (data) {
      closeModal();
      toast(t(groupId ? "toast.updated" : "toast.created", { name: data.group.name }));
      return refresh();
    }).catch(function (error) {
      if (error.status === 409 && error.payload && error.payload.critical) {
        criticalDialog(error.payload.critical, function () { saveGroup(payload, groupId, true); });
        return;
      }
      var box = $("#group-error");
      if (box) {
        box.textContent = error.message;
        box.classList.remove("hidden");
      } else {
        toast(error.message);
      }
    });
  }

  function openGroupEditor(group) {
    api("GET", "/api/users")
      .then(function (data) { openGroupEditorWith(group, data.users || []); })
      .catch(function () { openGroupEditorWith(group, []); });
  }

  function openGroupEditorWith(group, users) {
    group = group || {
      name: "", emoji: "⏳", weekday_minutes: 60, weekend_minutes: 60,
      services: [], domains: [], windows: [], paused: false, owner_id: null
    };
    var selected = { services: (group.services || []).slice() };
    var windows = (group.windows || []).map(function (w) {
      return { start: w.start, end: w.end, days: w.days };
    });

    function dayOptions(selectedDay) {
      var days = [
        { value: "all", label: t("groups.bedtime_days_all") },
        { value: "weekday", label: t("groups.bedtime_days_weekday") },
        { value: "weekend", label: t("groups.bedtime_days_weekend") }
      ];
      return days.map(function (day) {
        return '<option value="' + day.value + '"' +
          (day.value === selectedDay ? " selected" : "") + ">" + esc(day.label) + "</option>";
      }).join("");
    }

    function paintWindows() {
      var list = $("#bedtime-windows");
      if (!list) { return; }
      list.innerHTML = windows.map(function (w, index) {
        return '<div class="row tight" data-window="' + index + '">' +
          '<input type="time" data-window-start="' + index + '" value="' + esc(w.start) +
          '" aria-label="' + esc(t("groups.bedtime_from")) + '">' +
          '<input type="time" data-window-end="' + index + '" value="' + esc(w.end) +
          '" aria-label="' + esc(t("groups.bedtime_until")) + '">' +
          '<select data-window-days="' + index + '" aria-label="' +
          esc(t("groups.bedtime_days")) + '">' + dayOptions(w.days) + "</select>" +
          '<button type="button" class="btn ghost" data-action="remove-window" data-window="' +
          index + '">' + esc(t("groups.bedtime_remove")) + "</button></div>";
      }).join("");
      var add = document.querySelector("[data-action='add-window']");
      if (add) { add.disabled = windows.length >= 4; }
    }

    function readWindows() {
      var list = $("#bedtime-windows");
      if (!list) { return windows; }
      return windows.map(function (w, index) {
        var start = list.querySelector('[data-window-start="' + index + '"]');
        var end = list.querySelector('[data-window-end="' + index + '"]');
        var days = list.querySelector('[data-window-days="' + index + '"]');
        return {
          start: start ? start.value : w.start,
          end: end ? end.value : w.end,
          days: days ? days.value : w.days
        };
      });
    }

    function sheet() {
      var chips = selected.services.map(function (id) {
        return '<span class="chip">' + esc(id) + ' <button type="button" data-action="remove-service" data-service="' +
          esc(id) + '" aria-label="remove">×</button></span>';
      }).join(" ") || '<span class="muted small">' + esc(t("groups.services_help")) + "</span>";
      return (
        '<h2>' + esc(group.id ? t("groups.edit") : t("groups.add")) + "</h2>" +
        '<p class="alert bad hidden" id="group-error" role="alert"></p>' +
        '<form id="group-form" class="stack mt">' +
        '<div class="row"><div class="w-emoji"><label for="g-emoji">' + esc(t("groups.emoji")) +
        '</label><input type="text" id="g-emoji" value="' + esc(group.emoji || "") + '" maxlength="8"></div>' +
        '<div class="grow1"><label for="g-name">' + esc(t("groups.name")) + "</label>" +
        '<input type="text" id="g-name" value="' + esc(group.name) + '" required></div></div>' +
        '<div class="row"><div class="grow1"><label for="g-weekday">' + esc(t("groups.weekday")) +
        '</label><input type="number" id="g-weekday" min="0" max="1440" value="' +
        group.weekday_minutes + '"></div>' +
        '<div class="grow1"><label for="g-weekend">' + esc(t("groups.weekend")) +
        '</label><input type="number" id="g-weekend" min="0" max="1440" value="' +
        group.weekend_minutes + '"></div></div>' +
        '<fieldset class="bedtime"><legend>' + esc(t("groups.bedtime_heading")) + "</legend>" +
        '<p class="sub">' + esc(t("groups.bedtime_help")) + "</p>" +
        '<div class="stack" id="bedtime-windows"></div>' +
        '<button type="button" class="btn mt" data-action="add-window">' +
        esc(t("groups.bedtime_add")) + "</button></fieldset>" +
        '<div><label>' + esc(t("groups.services")) +
        '</label><div class="row tight" id="service-chips">' + chips +
        ' <button type="button" class="btn" data-action="pick-services">+ ' +
        esc(t("groups.services")) + "</button></div></div>" +
        '<div><label for="g-owner">' + esc(t("groups.owner")) + "</label>" +
        '<select id="g-owner"><option value="">' + esc(t("groups.owner_shared")) + "</option>" +
        users.map(function (u) {
          var sel = String(group.owner_id) === String(u.id) ? " selected" : "";
          return '<option value="' + u.id + '"' + sel + ">" + esc(u.username) + " (" +
            esc(u.role) + ")</option>";
        }).join("") + "</select></div>" +
        '<div><label for="g-domains">' + esc(t("groups.domains")) + "</label>" +
        '<textarea id="g-domains" placeholder="example.com">' +
        esc((group.domains || []).join("\n")) + "</textarea>" +
        '<p class="sub">' + esc(t("groups.domains_help")) + "</p></div>" +
        '<label class="check"><input type="checkbox" id="g-paused"' +
        (group.paused ? " checked" : "") + "> " + esc(t("groups.paused")) + "</label>" +
        '<div class="row"><button class="btn primary" type="submit">' +
        esc(t("common.save")) + "</button>" +
        '<button class="btn" type="button" data-action="modal-close">' +
        esc(t("common.cancel")) + "</button>" +
        (group.id ? '<button class="btn danger" type="button" data-action="delete-group" data-group="' +
          group.id + '">' + esc(t("common.delete")) + "</button>" : "") +
        (group.id ? '<button class="btn ghost" type="button" data-action="test-group" data-group="' +
          group.id + '">' + esc(t("groups.test")) + "</button>" : "") +
        "</div></form>"
      );
    }

    function paint() {
      windows = readWindows();
      openModal(sheet());
      paintWindows();
      bind();
    }

    function bind() {
      var form = $("#group-form");
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var payload = {
          name: $("#g-name").value,
          emoji: $("#g-emoji").value,
          weekday_minutes: Number($("#g-weekday").value),
          weekend_minutes: Number($("#g-weekend").value),
          domains_text: $("#g-domains").value,
          services: selected.services,
          windows: readWindows(),
          paused: $("#g-paused").checked,
          owner_id: $("#g-owner").value === "" ? null : Number($("#g-owner").value)
        };
        saveGroup(payload, group.id);
      });
      $$("[data-action='add-window']").forEach(function (button) {
        button.addEventListener("click", function () {
          windows = readWindows();
          if (windows.length >= 4) { return; }
          windows.push({ start: "21:00", end: "07:00", days: "all" });
          paintWindows();
        });
      });
      var windowList = $("#bedtime-windows");
      if (windowList) {
        windowList.addEventListener("click", function (event) {
          var target = event.target.closest("[data-action='remove-window']");
          if (!target) { return; }
          windows = readWindows();
          windows.splice(Number(target.dataset.window), 1);
          paintWindows();
        });
      }
      $$("[data-action='pick-services']").forEach(function (button) {
        button.addEventListener("click", function () {
          openServicePicker(selected.services, function (next) {
            selected.services = next;
            paint();
          });
        });
      });
      $$("[data-action='remove-service']").forEach(function (button) {
        button.addEventListener("click", function () {
          selected.services = selected.services.filter(function (id) {
            return id !== button.dataset.service;
          });
          paint();
        });
      });
    }
    paint();
  }

  function openServicePicker(current, done) {
    function sheet() {
      return (
        '<h2>' + esc(t("groups.services")) + "</h2>" +
        '<div class="search mt"><input type="search" id="service-search" placeholder="' +
        esc(t("groups.services")) + '" aria-label="search"></div>' +
        '<div class="list mt scroll-y" id="service-list"></div>' +
        '<div class="row mt"><button class="btn primary" id="service-done">' +
        esc(t("common.save")) + "</button>" +
        '<button class="btn" data-action="modal-close">' + esc(t("common.cancel")) + "</button></div>"
      );
    }
    openModal(sheet());
    var selected = current.slice();
    var all = [];

    function paint(list) {
      $("#service-list").innerHTML = list.map(function (service) {
        var checked = selected.indexOf(service.id) >= 0;
        var icon = service.icon_svg
          ? '<img alt="" width="20" height="20" src="data:image/svg+xml;base64,' +
            esc(service.icon_svg) + '">'
          : "";
        return '<label class="item"><input type="checkbox" data-service="' + esc(service.id) + '"' +
          (checked ? " checked" : "") + (service.group ? " disabled" : "") + "> " + icon +
          ' <span class="grow">' + esc(service.name) +
          (service.group ? ' <span class="muted small">(' + esc(service.group) + ")</span>" : "") +
          "</span></label>";
      }).join("") || '<p class="muted small">…</p>';
      $$("#service-list input[type=checkbox]").forEach(function (box) {
        box.addEventListener("change", function () {
          var id = box.dataset.service;
          if (box.checked) {
            if (selected.indexOf(id) < 0) { selected.push(id); }
          } else {
            selected = selected.filter(function (s) { return s !== id; });
          }
        });
      });
    }

    function load() {
      if (servicesCache) { all = servicesCache; paint(all); return; }
      $("#service-list").innerHTML = '<p class="muted small">' + esc(t("common.loading")) + "</p>";
      api("GET", "/api/services").then(function (data) {
        servicesCache = data.services || [];
        all = servicesCache;
        paint(all);
      }).catch(function (error) { $("#service-list").textContent = error.message; });
    }

    $("#service-search").addEventListener("input", function (event) {
      var q = event.target.value.toLowerCase();
      paint(all.filter(function (s) {
        return s.id.indexOf(q) >= 0 || s.name.toLowerCase().indexOf(q) >= 0;
      }));
    });

    $("#service-done").addEventListener("click", function () { closeModal(); done(selected); });
    load();
  }

  function deleteGroup(groupId, name) {
    openModal(
      '<h2>' + esc(t("common.delete")) + " — " + esc(name) + "</h2>" +
      '<p class="sub">' + esc(t("groups.delete_warning")) + "</p>" +
      '<div class="row mt"><button class="btn" data-action="modal-close">' +
      esc(t("common.cancel")) + "</button>" +
      '<button class="btn danger" id="delete-confirm">' + esc(t("common.delete")) + "</button></div>"
    );
    $("#delete-confirm").addEventListener("click", function (event) {
      var button = event.currentTarget;
      button.disabled = true;
      api("DELETE", "/api/groups/" + groupId).then(function () {
        closeModal();
        toast(t("toast.deleted", { name: name }));
        return refresh();
      }).catch(function (error) {
        button.disabled = false;
        toast(error.message);
      });
    });
  }

  function testGroup(groupId) {
    closeModal();
    api("GET", "/api/groups/" + groupId + "/test").then(function (data) {
      var rows = data.results.map(function (r) {
        return "<tr><td>" + esc(r.host) + '</td><td><span class="pill ' +
          (r.blocked ? "bad" : "ok") + '">' +
          esc(t(r.blocked ? "groups.test_blocked" : "groups.test_allowed")) +
          "</span></td></tr>";
      }).join("");
      openModal(
        '<h2>' + esc(t("groups.test")) + "</h2>" +
        '<div class="table-wrap mt"><table><thead><tr><th>' +
        esc(t("groups.test_host")) + "</th><th>" + esc(t("groups.test_adguard")) +
        "</th></tr></thead><tbody>" +
        rows + "</tbody></table></div>" +
        '<div class="row mt"><button class="btn" data-action="modal-close">' +
        esc(t("common.close")) + "</button></div>"
      );
    }).catch(function (error) { toast(error.message); });
  }

  // ------------------------------------------------------------------ users
  function renderUsers() {
    var list = $("#user-list");
    if (!list || !state.users) { return; }
    if (!state.users.length) {
      list.innerHTML = '<p class="muted small">' + esc(t("users.none")) + "</p>";
      return;
    }
    list.innerHTML = state.users.map(function (u) {
      var isAdmin = u.role === "admin";
      return '<div class="item">' +
        '<div class="grow"><div class="name">' + esc(u.username) +
        ' <span class="pill ' + (isAdmin ? "ok" : "muted") + '">' +
        esc(t("users.role." + u.role)) + "</span>" +
        (u.disabled ? ' <span class="pill bad">' + esc(t("users.disabled")) + "</span>" : "") +
        "</div>" +
        '<div class="muted small">' + (u.last_login
          ? t("users.last_login", { time: clock(u.last_login) })
          : esc(t("users.never"))) + "</div></div>" +
        '<div class="row tight">' +
        '<button class="btn ghost" data-action="user-password" data-user="' + u.id +
        '" data-username="' + esc(u.username) + '">' + esc(t("users.reset")) + "</button>" +
        '<button class="btn ghost" data-action="user-role" data-user="' + u.id + '" data-role="' +
        (isAdmin ? "user" : "admin") + '">' +
        esc(isAdmin ? t("users.role.user") : t("users.role.admin")) + "</button>" +
        '<button class="btn ghost" data-action="user-toggle" data-user="' + u.id +
        '" data-disabled="' + (u.disabled ? "0" : "1") + '">' +
        esc(u.disabled ? t("users.enable") : t("users.disable")) + "</button>" +
        '<button class="btn danger" data-action="user-delete" data-user="' + u.id +
        '" data-username="' + esc(u.username) + '">' + esc(t("users.delete")) + "</button>" +
        "</div></div>";
    }).join("");
  }

  function loadUsers() {
    return api("GET", "/api/users").then(function (data) {
      state.users = data.users || [];
      renderUsers();
    }).catch(function (error) { toast(error.message); });
  }

  // ----------------------------------------------------------------- alerts
  function alertHost(host) {
    var text = String(host || "");
    if (!text) { return ""; }
    if (text.charAt(0) === "[") { return text.slice(1).split("]")[0].toLowerCase(); }
    return text.split(":")[0].toLowerCase();
  }

  function alertHostIsLocal(host) {
    var name = alertHost(host);
    if (!name) { return false; }
    if (name === "localhost" || name === "::1" || name === "0.0.0.0") { return true; }
    if (/^127\./.test(name) || /^10\./.test(name) || /^192\.168\./.test(name)) { return true; }
    var private172 = name.match(/^172\.(\d+)\./);
    if (private172 && Number(private172[1]) >= 16 && Number(private172[1]) <= 31) { return true; }
    if (/^f[cd][0-9a-f]{2}:/.test(name) || /^fe80:/.test(name)) { return true; }
    return false;
  }

  function alertReason(category, channel) {
    var text = String(category || "");
    var via = String(channel || "");
    if (text.indexOf("http_") === 0) {
      if (via === "telegram") {
        if (text === "http_401") { return t("alerts.reason_telegram_token"); }
        if (text === "http_400" || text === "http_403" || text === "http_404") {
          return t("alerts.reason_telegram_delivery");
        }
      }
      return t("alerts.reason_http");
    }
    if (text === "dns" || text === "connect") { return t("alerts.reason_connect"); }
    if (text === "timeout") { return t("alerts.reason_timeout"); }
    if (text === "config") { return t("alerts.reason_config"); }
    if (text === "rate_limited") { return t("alerts.reason_limited"); }
    return t("alerts.reason_other");
  }

  function alertResultText(data) {
    if (!data.last_result || !data.last_result_ts) { return ""; }
    if (data.last_result === "ok") {
      return t("alerts.result_ok", { time: clock(data.last_result_ts) });
    }
    return t("alerts.result_failed", {
      reason: alertReason(data.last_result, data.channel),
      time: clock(data.last_result_ts)
    });
  }

  function paintAlerts(data) {
    alertsConfig = data || null;
    if (!data) { return; }
    var channel = data.channel || "";
    $("#alerts-channel").value = channel;
    $("#alerts-enabled").checked = data.enabled !== false;
    $("#alerts-telegram-token").placeholder = data.telegram.token_tail
      ? t("alerts.saved_tail", { tail: data.telegram.token_tail })
      : t("alerts.telegram_token");
    $("#alerts-telegram-chat").placeholder = data.telegram.chat_tail
      ? t("alerts.saved_tail", { tail: data.telegram.chat_tail })
      : t("alerts.telegram_chat");
    $("#alerts-ntfy-base").placeholder = data.ntfy.base_host || "https://ntfy.sh";
    $("#alerts-ntfy-topic").placeholder = data.ntfy.topic_tail
      ? t("alerts.saved_tail", { tail: data.ntfy.topic_tail })
      : t("alerts.topic");
    $("#alerts-ntfy-token").placeholder = data.ntfy.token_configured
      ? t("alerts.saved_plain")
      : t("alerts.token");
    $("#alerts-webhook-url").placeholder = !data.webhook.host
      ? "https://example/hook"
      : data.webhook.path_tail
        ? t("alerts.saved_tail", { tail: data.webhook.path_tail })
        : t("alerts.saved_plain");
    $$("#alerts-events input[data-event]").forEach(function (box) {
      box.checked = Boolean(data.events && data.events[box.dataset.event]);
    });
    $("#alerts-telegram").classList.toggle("hidden", channel !== "telegram");
    $("#alerts-ntfy").classList.toggle("hidden", channel !== "ntfy");
    $("#alerts-webhook").classList.toggle("hidden", channel !== "webhook");
    $$("[data-action='clear-secret']").forEach(function (button) {
      var secret = button.dataset.secret;
      var configured = {
        telegram_token: Boolean(data.telegram.token_tail),
        ntfy_token: Boolean(data.ntfy.token_configured),
        webhook_url: Boolean(data.webhook.host)
      }[secret];
      button.classList.toggle("hidden", !configured);
    });
    var needsToken = channel === "ntfy" && !data.ntfy.token_configured &&
      !alertHostIsLocal(data.ntfy.base_host);
    var warning = $("#alerts-warning");
    warning.textContent = needsToken ? t("alerts.token_warning") : "";
    warning.classList.toggle("hidden", !needsToken);
    $("#alerts-result").textContent = alertResultText(data);
    paintAlertsHint();
  }

  /* One calm line on the Today tab, for an admin who never opened the alerts
     card: the config is fetched once, and never for a kid account. */
  function paintAlertsHint() {
    var el = $("#alerts-hint");
    if (!el) { return; }
    var config = alertsConfig;
    var wanted = (!config || !config.channel || config.enabled === false) &&
      !hintDismissed();
    el.classList.toggle("hidden", !(role === "admin" && wanted));
  }

  /* Storage can throw (private mode, blocked site data); failing only means
     the reminder is back on the next page, never a broken Today tab. */
  function hintHiddenAt() {
    var at = 0;
    try {
      var raw = window.localStorage.getItem(HINT_HIDDEN_KEY);
      var stored = Number(raw);
      if (raw && isFinite(stored) && stored > 0) { at = stored; }
    } catch (error) {
      at = 0;
    }
    return Math.max(at, hintHiddenFor);
  }

  function hintDismissed() {
    var at = hintHiddenAt();
    return at > 0 && Date.now() - at < HINT_HIDDEN_MS;
  }

  function rememberHintDismissal() {
    hintHiddenFor = Date.now();
    try {
      window.localStorage.setItem(HINT_HIDDEN_KEY, String(Date.now()));
    } catch (error) {
      /* no storage: the banner stays hidden for this view only */
    }
  }

  function maybeLoadAlertsHint() {
    if (role !== "admin" || alertsHintLoaded) { return; }
    alertsHintLoaded = true;
    api("GET", "/api/alerts")
      .then(function (data) {
        alertsConfig = data;
        paintAlertsHint();
      })
      .catch(function () { alertsHintLoaded = false; });
  }

  function loadAlerts() {
    return api("GET", "/api/alerts")
      .then(paintAlerts)
      .catch(function (error) { toast(error.message); });
  }

  function alertsPayload() {
    var payload = {
      channel: $("#alerts-channel").value,
      enabled: $("#alerts-enabled").checked,
      events: {}
    };
    $$("#alerts-events input[data-event]").forEach(function (box) {
      payload.events[box.dataset.event] = box.checked;
    });
    [
      ["telegram_token", "#alerts-telegram-token"],
      ["telegram_chat", "#alerts-telegram-chat"],
      ["ntfy_base", "#alerts-ntfy-base"],
      ["ntfy_topic", "#alerts-ntfy-topic"],
      ["ntfy_token", "#alerts-ntfy-token"],
      ["webhook_url", "#alerts-webhook-url"]
    ].forEach(function (field) {
      var input = $(field[1]);
      var value = input ? input.value.trim() : "";
      if (value) { payload[field[0]] = value; }
    });
    return payload;
  }

  // --------------------------------------------------------------- discover
  function renderDiscover(data) {
    discoverData = data;
    var select = $("#discover-client");
    var current = select.value;
    select.innerHTML = '<option value="">' + esc(t("discover.all_clients")) + "</option>" +
      data.clients.map(function (c) {
        return '<option value="' + esc(c.client) + '">' + esc(c.client) + "</option>";
      }).join("");
    select.value = current;

    if (!data.domains.length) {
      $("#discover-results").innerHTML = '<p class="alert">' + esc(t("discover.none")) +
        "<br>" + esc(t("discover.empty_hint")) + "</p>";
      return;
    }
    var rows = data.domains.map(function (d) {
      return '<tr><td><input type="checkbox" data-domain="' + esc(d.domain) +
        '" aria-label="' + esc(d.domain) + '"></td>' +
        "<td>" + esc(d.domain) + "</td><td>" + d.count + "</td><td>" +
        esc(t("discover.last_seen", { ago: d.last_seen_ago })) + "</td></tr>";
    }).join("");
    $("#discover-results").innerHTML =
      '<div class="table-wrap"><table><thead><tr><th></th><th>' +
      esc(t("discover.domain")) + "</th><th>" + esc(t("discover.queries")) + "</th><th>" +
      esc(t("discover.seen")) + "</th></tr></thead><tbody>" + rows + "</tbody></table></div>" +
      '<div class="row mt"><select id="discover-target" aria-label="' +
      esc(t("discover.add_to")) + '">' +
      state.groups.map(function (g) {
        return '<option value="' + g.id + '">' + esc((g.emoji ? g.emoji + " " : "") + g.name) +
          "</option>";
      }).join("") +
      '</select><button class="btn primary" id="discover-add">' + esc(t("discover.add")) +
      "</button></div>";
    $("#discover-add").addEventListener("click", function () {
      var checked = $$("#discover-results input[type=checkbox]:checked").map(function (box) {
        return box.dataset.domain;
      });
      if (!checked.length) { return; }
      var groupId = Number($("#discover-target").value);
      api("POST", "/api/discover/add", { group_id: groupId, domains: checked }).then(function (data) {
        toast(t("toast.domains_added", { count: data.added.length, name: data.group.name }));
        return refresh().then(runDiscover);
      }).catch(function (error) {
        if (error.status === 409 && error.payload && error.payload.critical) {
          criticalDialog(error.payload.critical, function () {
            api("POST", "/api/discover/add", {
              group_id: groupId, domains: checked, confirm_critical: true
            }).then(function () {
              toast(t("common.saved"));
              return refresh().then(runDiscover);
            }).catch(function (e) { toast(e.message); });
          });
          return;
        }
        toast(error.message);
      });
    });
  }

  function runDiscover() {
    var minutes = Number($("#discover-minutes").value);
    var client = $("#discover-client").value;
    var showAll = $("#discover-showall").checked;
    $("#discover-results").innerHTML = '<p class="muted small">' + esc(t("common.loading")) + "</p>";
    api("GET", "/api/discover?minutes=" + minutes + "&client=" + encodeURIComponent(client) +
      "&show_all=" + (showAll ? "true" : "false"))
      .then(renderDiscover)
      .catch(function (error) {
        $("#discover-results").innerHTML = '<p class="alert bad">' + esc(error.message) + "</p>";
      });
  }

  /* The server zone decides "tomorrow", so the hint is refreshed here and not
     recomputed from the browser clock. */
  function noteFreeTime(event) {
    var input = event.target && event.target.closest
      ? event.target.closest("[data-free-time]")
      : null;
    if (!input) { return; }
    var groupId = String(input.dataset.freeTime);
    freeTimeChoice[groupId] = input.value;
    var hint = document.querySelector('[data-free-tomorrow="' + groupId + '"]');
    if (hint) { hint.classList.toggle("hidden", !freeTimeIsTomorrow(input.value)); }
  }

  document.addEventListener("input", noteFreeTime);
  document.addEventListener("change", noteFreeTime);

  // ---------------------------------------------------------------- wiring
  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-action]");
    if (!target) { return; }
    /* A busy card keeps its focus: the tap is ignored here instead of being
       blocked by the disabled attribute, which would drop focus to <body>. */
    if (target.getAttribute("aria-disabled") === "true") { return; }
    var action = target.dataset.action;
    var groupId = target.dataset.group ? Number(target.dataset.group) : null;
    var group = groupId ? findGroup(groupId) : null;
    var name = group ? group.name : "";

    if (action === "mode") {
      var to = target.dataset.mode || "";
      if (to === "free") {
        /* Free is two-step: the tap only opens the time panel, and nothing is
           sent until Allow is pressed. A group that is already free keeps its
           panel open, so a tap on it changes nothing. */
        pendingFree[String(groupId)] = group ? modeFor(group) !== "free" : false;
        paintMode(target.closest("[data-card-group]"), group);
        return;
      }
      pendingFree[String(groupId)] = false;
      setMode(groupId, to);
    } else if (action === "free-apply") {
      var freeInput = document.querySelector('[data-free-time="' + groupId + '"]');
      var until = freeTimeChoice[String(groupId)] || defaultFreeTime() ||
        (freeInput ? freeInput.value : "");
      setMode(groupId, "free", until);
    } else if (action === "free-cancel") {
      pendingFree[String(groupId)] = false;
      paintMode(target.closest("[data-card-group]"), group);
    } else if (action === "resume") {
      adminAction("pause", groupId, { paused: false }, t("toast.resumed", { name: name }));
    } else if (action === "custom") {
      openCustom[String(groupId)] = !openCustom[String(groupId)];
      paintCustom(target.closest("[data-card-group]"), groupId);
    } else if (action === "bonus") {
      var minutes = Number(target.dataset.minutes || 15);
      adminAction("bonus", groupId, { minutes: minutes }, bonusMessage(name, minutes));
    } else if (action === "bonus-custom") {
      var input = document.querySelector('[data-custom-bonus="' + groupId + '"]');
      var value = input ? Number(input.value) : NaN;
      if (!input || !input.value || !Number.isInteger(value) || value === 0 ||
          Math.abs(value) > 1440) {
        toast(t("admin.custom_invalid"));
        return;
      }
      adminAction("bonus", groupId, { minutes: value }, bonusMessage(name, value), function () {
        /* A number left in the box counts as "typing" and would freeze this
           card's controls, so it goes only once the server accepted it. */
        input.value = "";
        openCustom[String(groupId)] = false;
        paintCustom(input.closest("[data-card-group]"), groupId);
      });
    } else if (action === "reset") {
      resetGroup(groupId, name);
    } else if (action === "hint-dismiss") {
      rememberHintDismissal();
      paintAlertsHint();
    } else if (action === "user-password") {
      resetUserPassword(Number(target.dataset.user), target.dataset.username);
    } else if (action === "user-role") {
      api("POST", "/api/users/" + target.dataset.user + "/role", { role: target.dataset.role })
        .then(function () { toast(t("common.saved")); loadUsers(); })
        .catch(function (error) { toast(error.message); });
    } else if (action === "user-toggle") {
      api("POST", "/api/users/" + target.dataset.user + "/disabled", {
        disabled: target.dataset.disabled === "1"
      }).then(function () { toast(t("common.saved")); loadUsers(); })
        .catch(function (error) { toast(error.message); });
    } else if (action === "user-delete") {
      deleteUser(Number(target.dataset.user), target.dataset.username);
    } else if (action === "edit") {
      if (group) { openGroupEditor(group); }
    } else if (action === "pause") {
      api("POST", "/api/groups/" + groupId + "/pause", {
        paused: target.dataset.paused === "1"
      }).then(function (data) {
        toast(t(data.group.paused ? "toast.paused" : "toast.resumed", { name: data.group.name }));
        return refresh();
      }).catch(function (error) { toast(error.message); });
    } else if (action === "test-group") {
      testGroup(groupId);
    } else if (action === "delete-group") {
      deleteGroup(groupId, name);
    } else if (action === "modal-close") {
      closeModal();
    }
  });

  document.addEventListener("keydown", function (event) {
    var modal = $("#modal");
    if (!modal || modal.classList.contains("hidden")) { return; }
    if (event.key === "Escape") {
      event.preventDefault();
      closeModal();
      return;
    }
    if (event.key !== "Tab") { return; }
    var items = modalFocusable();
    if (!items.length) { return; }
    var first = items[0];
    var last = items[items.length - 1];
    var active = document.activeElement;
    var inside = modal.contains(active);
    if (event.shiftKey && (active === first || !inside)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !inside)) {
      event.preventDefault();
      first.focus();
    }
  });

  $$("#admin-tabs .tab").forEach(function (button) {
    button.addEventListener("click", function () {
      if (button.dataset.tab !== tab) { alertsTabLoaded = false; }
      tab = button.dataset.tab;
      if (tab === "history") { loadUsage(); }
      render();
    });
  });

  var newGroupButton = $("#new-group");
  if (newGroupButton) {
    newGroupButton.addEventListener("click", function () { openGroupEditor(null); });
  }

  var historyDaysSelect = $("#history-days");
  if (historyDaysSelect) {
    historyDaysSelect.addEventListener("change", function () {
      usageDays = Number(historyDaysSelect.value) || 7;
      loadUsage();
    });
  }

  var discoverButton = $("#discover-run");
  if (discoverButton) { discoverButton.addEventListener("click", runDiscover); }

  var logoutButton = $("#logout");
  if (logoutButton) {
    logoutButton.addEventListener("click", function () {
      api("POST", "/api/logout", {}).then(function () {
        window.location.href = base + "/login";
      });
    });
  }

  var accountForm = $("#account-password-form");
  if (accountForm) {
    accountForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var current = $("#current-password").value;
      var next = $("#account-new-password").value;
      var repeat = $("#account-repeat-password").value;
      if (next !== repeat) { toast(t("toast.password_mismatch")); return; }
      api("POST", "/api/account/password", {
        current_password: current,
        new_password: next
      }).then(function () {
        accountForm.reset();
        toast(t("toast.password"));
      }).catch(function (error) { toast(error.message); });
    });
  }

  var alertsForm = $("#alerts-form");
  if (alertsForm) {
    alertsForm.addEventListener("submit", function (event) {
      event.preventDefault();
      api("PUT", "/api/alerts", alertsPayload()).then(function (data) {
        [
          "#alerts-telegram-token",
          "#alerts-telegram-chat",
          "#alerts-ntfy-base",
          "#alerts-ntfy-topic",
          "#alerts-ntfy-token",
          "#alerts-webhook-url"
        ].forEach(function (selector) { var input = $(selector); if (input) { input.value = ""; } });
        paintAlerts(data);
        toast(t("common.saved"));
      }).catch(function (error) { toast(error.message); });
    });
    var channelSelect = $("#alerts-channel");
    channelSelect.addEventListener("change", function () {
      $("#alerts-telegram").classList.toggle("hidden", channelSelect.value !== "telegram");
      $("#alerts-ntfy").classList.toggle("hidden", channelSelect.value !== "ntfy");
      $("#alerts-webhook").classList.toggle("hidden", channelSelect.value !== "webhook");
    });
    $$("[data-action='clear-secret']").forEach(function (button) {
      button.addEventListener("click", function () {
        var secret = button.dataset.secret;
        var payload = {};
        payload[secret] = "";
        api("PUT", "/api/alerts", payload).then(function (data) {
          var input = $({
            telegram_token: "#alerts-telegram-token",
            ntfy_token: "#alerts-ntfy-token",
            webhook_url: "#alerts-webhook-url"
          }[secret]);
          if (input) { input.value = ""; }
          paintAlerts(data);
          toast(t("common.saved"));
        }).catch(function (error) { toast(error.message); });
      });
    });
    var alertsTest = $("#alerts-test");
    alertsTest.addEventListener("click", function () {
      alertsTest.disabled = true;
      api("POST", "/api/alerts/test", {}).then(function (data) {
        var channel = alertsConfig ? alertsConfig.channel : "";
        $("#alerts-result").textContent = data.ok
          ? t("alerts.test_ok")
          : t("alerts.test_failed", { reason: alertReason(data.category, channel) });
      }).catch(function (error) {
        $("#alerts-result").textContent = error.message;
      }).then(function () { alertsTest.disabled = false; });
    });
  }

  var alertsSetup = $("#alerts-setup");
  if (alertsSetup) {
    alertsSetup.addEventListener("click", function () {
      tab = "settings";
      alertsTabLoaded = false;
      render();
      var card = $("#alerts-card");
      if (card && card.scrollIntoView) { card.scrollIntoView({ block: "start" }); }
      var channel = $("#alerts-channel");
      if (channel) { channel.focus(); }
    });
  }

  var userForm = $("#user-form");
  if (userForm) {
    userForm.addEventListener("submit", function (event) {
      event.preventDefault();
      api("POST", "/api/users", {
        username: $("#new-username").value.trim(),
        password: $("#new-user-password").value,
        role: $("#new-user-role").value
      }).then(function (data) {
        toast(t("users.created", { name: data.user.username }));
        $("#new-username").value = "";
        $("#new-user-password").value = "";
        loadUsers();
      }).catch(function (error) { toast(error.message); });
    });
  }

  function resetUserPassword(userId, username) {
    openModal(
      '<h2 class="wrap-anywhere">' + esc(t("users.reset")) + " — " + esc(username) + "</h2>" +
      '<form id="user-password-form" class="stack mt">' +
      '<input type="password" id="user-password" autocomplete="new-password" placeholder="' +
      esc(t("users.password")) + '" aria-label="' + esc(t("users.password")) + '">' +
      '<p class="sub">' + esc(t("users.password_hint")) + "</p>" +
      '<div class="row"><button class="btn primary" type="submit">' + esc(t("common.save")) +
      '</button><button class="btn" type="button" data-action="modal-close">' +
      esc(t("common.cancel")) + "</button></div></form>"
    );
    $("#user-password-form").addEventListener("submit", function (event) {
      event.preventDefault();
      api("POST", "/api/users/" + userId + "/password", { password: $("#user-password").value })
        .then(function () {
          closeModal();
          toast(t("toast.user_password", { name: username }));
          loadUsers();
        })
        .catch(function (error) { toast(error.message); });
    });
  }

  function deleteUser(userId, username) {
    openModal(
      '<h2 class="wrap-anywhere">' + esc(t("users.delete")) + " — " + esc(username) + "</h2>" +
      '<p class="sub">' + esc(t("common.confirm")) + "</p>" +
      '<div class="row mt"><button class="btn" data-action="modal-close">' +
      esc(t("common.cancel")) + "</button>" +
      '<button class="btn danger" id="user-delete-confirm">' + esc(t("common.delete")) +
      "</button></div>"
    );
    $("#user-delete-confirm").addEventListener("click", function () {
      api("DELETE", "/api/users/" + userId).then(function () {
        closeModal();
        toast(t("users.deleted", { name: username }));
        loadUsers();
      }).catch(function (error) { toast(error.message); });
    });
  }

  var adguardTest = $("#adguard-test");
  if (adguardTest) {
    adguardTest.addEventListener("click", function () {
      api("POST", "/api/adguard/test", {}).then(function (data) {
        $("#adguard-result").textContent = data.ok
          ? t("admin.adguard_ok", { detail: data.detail })
          : t("admin.adguard_fail", { detail: data.detail });
      }).catch(function (error) { $("#adguard-result").textContent = error.message; });
    });
  }

  /* 5s polling: patched in place, never a full re-render of the cards. */
  window.setInterval(function () { if (state) { refresh(); } }, 5000);

  // boot
  fetch(base + "/api/i18n", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (data) { i18n = data || {}; })
    .catch(function () { i18n = {}; })
    .then(function () {
      return fetch(base + "/api/meta", { credentials: "same-origin" }).then(function (r) {
        return r.json();
      });
    })
    .then(function (meta) {
      if (!meta || !meta.role) { window.location.href = base + "/login"; return null; }
      if (meta.role !== "admin") { window.location.href = base + "/"; return null; }
      return refresh();
    })
    .catch(function () { window.location.href = base + "/login"; });
}());
