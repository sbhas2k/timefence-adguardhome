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
  var servicesCache = null;
  var discoverData = null;

  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function t(key, vars) {
    var text = i18n[key] || key;
    if (vars) {
      Object.keys(vars).forEach(function (k) {
        text = text.replace(new RegExp("\\{" + k + "\\}", "g"), vars[k]);
      });
    }
    return text;
  }

  function fmtClock(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = seconds % 60;
    if (h > 0) {
      return h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
    }
    return m + ":" + String(s).padStart(2, "0");
  }

  function fmtTime(ts) {
    if (!ts) { return ""; }
    return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function fmtMinutes(value) {
    var n = Math.round(Number(value) * 10) / 10;
    return (n % 1 === 0 ? String(n) : n.toFixed(1));
  }

  function toast(message) {
    var el = $("#toast");
    el.textContent = message;
    el.hidden = false;
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(function () { el.hidden = true; }, 3200);
  }

  function alertBox(message) {
    var el = $("#global-alert");
    if (!message) { el.classList.add("hidden"); return; }
    el.textContent = message;
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
    }
  }

  function openModal(html) {
    var modal = $("#modal");
    $("#modal-sheet").innerHTML = html;
    modal.classList.remove("hidden");
    var focusable = $("#modal-sheet").querySelector("input, textarea, button, select");
    if (focusable) { focusable.focus(); }
  }

  function closeModal() {
    $("#modal").classList.add("hidden");
    $("#modal-sheet").innerHTML = "";
  }

  // ------------------------------------------------------------------ render
  function statusFor(group, now) {
    if (group.paused) { return { cls: "warn", text: t("status.paused") }; }
    if (group.force_block_until) { return { cls: "bad", text: t("status.forced") }; }
    if (group.active_session) {
      return { cls: "ok", text: t("status.allowed", { time: fmtClock(group.active_session.ends_at - now) }), ends: group.active_session.ends_at };
    }
    if (group.free_until) {
      return { cls: "warn", text: t("status.free", { time: fmtTime(group.free_until) }) };
    }
    if (group.mode === "auto") {
      if (group.remaining_minutes <= 0) { return { cls: "bad", text: t("status.limit_reached") }; }
      return { cls: "ok", text: t("status.allowed_auto", { minutes: fmtMinutes(group.remaining_minutes) }) };
    }
    return { cls: "bad", text: t("status.blocked") };
  }

  function startButtons(group) {
    var remaining = group.remaining_minutes;
    var options = (state.app.session_options || [15, 30, 60]).filter(function (m) { return m <= remaining; });
    var html = options.map(function (m) {
      return '<button class="btn" data-action="start" data-group="' + group.id + '" data-minutes="' + m + '">' + m + " " + esc(t("common.minutes")) + "</button>";
    }).join("");
    if (!options.length && remaining >= 1) {
      var minutes = Math.floor(remaining);
      html = '<button class="btn" data-action="start" data-group="' + group.id + '" data-minutes="' + minutes + '">' + minutes + " " + esc(t("common.minutes")) + "</button>";
    }
    if (!html) {
      html = '<span class="pill muted">' + esc(t("session.done")) + "</span>";
    }
    return html;
  }

  function groupCard(group, admin, now) {
    var status = statusFor(group, now);
    var total = group.minutes_today + group.bonus_minutes;
    var usedPercent = total > 0 ? Math.min(100, (group.used_minutes / total) * 100) : 0;
    var leftover = total - group.used_minutes;
    var controls = "";
    if (group.mode === "session" && group.active_session) {
      controls = '<button class="btn danger big-btn" data-action="stop" data-group="' + group.id + '">' + esc(t("session.stop")) + "</button>";
    } else if (group.mode === "session" && !group.paused && !group.force_block_until) {
      controls = startButtons(group);
    } else if (group.mode === "auto" && !group.paused) {
      controls = '<div class="muted small">' + esc(t("groups.mode.auto")) + "</div>";
    }
    var adminRow = "";
    if (admin) {
      adminRow =
        '<div class="row tight mt">' +
        '<button class="btn" data-action="block" data-group="' + group.id + '">' + esc(t("admin.block_now")) + "</button>" +
        '<button class="btn" data-action="bonus" data-group="' + group.id + '" data-minutes="-15">' + esc(t("admin.decrease")) + "</button>" +
        '<button class="btn" data-action="bonus" data-group="' + group.id + '" data-minutes="15">' + esc(t("admin.bonus")) + "</button>" +
        '<input type="number" inputmode="numeric" step="5" data-custom-bonus="' + group.id + '" placeholder="' + esc(t("admin.custom_placeholder")) + '" aria-label="' + esc(t("admin.custom_placeholder")) + '">' +
        '<button class="btn" data-action="bonus-custom" data-group="' + group.id + '">' + esc(t("admin.apply")) + "</button>" +
        (group.force_block_until ? '<button class="btn" data-action="unblock" data-group="' + group.id + '">' + esc(t("toast.unblocked", { name: "" })) + "</button>" : "") +
        "</div>" +
        '<div class="row tight mt">' +
        '<input type="time" data-free-time="' + group.id + '" value="21:00" aria-label="' + esc(t("admin.free_label")) + '">' +
        '<button class="btn" data-action="free" data-group="' + group.id + '">' + esc(t("admin.free_apply")) + "</button>" +
        (group.free_until ? '<button class="btn" data-action="clearfree" data-group="' + group.id + '">' + esc(t("admin.clear_free")) + "</button>" : "") +
        '<button class="btn ghost" data-action="reset" data-group="' + group.id + '">' + esc(t("admin.reset_today")) + "</button>" +
        "</div>";
    }
    return (
      '<article class="card" data-card-group="' + group.id + '">' +
      '<h2><span aria-hidden="true">' + esc(group.emoji || "⏳") + "</span> " + esc(group.name) +
      ' <span class="pill ' + status.cls + '" data-status="' + group.id + '"' +
      (status.ends ? ' data-ends="' + status.ends + '" data-prefix="' + esc(t("status.allowed_prefix")) + '"' : "") +
      ">" + esc(status.text) + "</span></h2>" +
      '<div class="sub wrap-anywhere">' +
      (group.services.length ? "AdGuard: " + esc(group.services.join(", ")) : "") +
      (group.domains.length ? (group.services.length ? " · " : "") + esc(group.domains.join(", ")) : "") +
      "</div>" +
      '<div class="between mt"><div class="big">' + fmtMinutes(group.remaining_minutes) +
      '<span class="muted small"> ' + esc(t("common.minutes")) + "</span></div>" +
      '<div class="muted small center">' + esc(t("budget.left", { left: fmtMinutes(Math.max(0, leftover)), total: fmtMinutes(total) })) +
      "<br>" + esc(t("budget.used", { used: fmtMinutes(group.used_minutes) })) + "</div></div>" +
      '<progress max="100" value="' + usedPercent.toFixed(1) + '" aria-label="' + esc(t("budget.used", { used: fmtMinutes(group.used_minutes) })) + '"></progress>' +
      '<div class="row mt">' + controls + "</div>" +
      adminRow +
      "</article>"
    );
  }

  function renderToday() {
    var now = state.app.now;
    var groups = state.groups;
    $("#groups").innerHTML = groups.map(function (g) { return groupCard(g, role === "admin" && tab === "today", now); }).join("");
    $("#no-groups").classList.toggle("hidden", groups.length > 0);
  }

  function renderGroupList() {
    var list = $("#group-list");
    if (!state.groups.length) {
      list.innerHTML = '<p class="muted small">' + esc(t("groups.empty")) + "</p>";
      return;
    }
    list.innerHTML = state.groups.map(function (g) {
      return (
        '<div class="item">' +
        '<span aria-hidden="true">' + esc(g.emoji || "⏳") + "</span>" +
        '<div class="grow"><div class="name">' + esc(g.name) + "</div>" +
        '<div class="muted small">' + (g.mode === "auto" ? "auto" : "sessions") + " · " + g.weekday_minutes + "/" + g.weekend_minutes + " " + esc(t("common.minutes")) +
        " · " + esc(g.owner || t("groups.owner_shared")) +
        " · " + esc([].concat(g.services, g.domains).join(", ").slice(0, 70)) + "</div></div>" +
        (g.paused ? '<span class="pill warn">' + esc(t("status.paused")) + "</span>" : "") +
        '<button class="btn ghost" data-action="edit" data-group="' + g.id + '">' + esc(t("common.edit")) + "</button>" +
        '<button class="btn ghost" data-action="pause" data-group="' + g.id + '" data-paused="' + (g.paused ? "0" : "1") + '">' +
        (g.paused ? "▶" : "⏸") + "</button>" +
        "</div>"
      );
    }).join("");
  }

  function renderHistory() {
    if (!state) { return; }
    var names = {};
    state.groups.forEach(function (g) { names[g.id] = g; });
    var head = '<thead><tr><th>' + esc(t("history.day")) + "</th>" +
      state.groups.map(function (g) { return "<th>" + esc((g.emoji ? g.emoji + " " : "") + g.name) + "</th>"; }).join("") +
      "</tr></thead>";
    var rows = state.history.map(function (day) {
      return "<tr><td>" + esc(day.day) + "</td>" +
        state.groups.map(function (g) {
          var value = day[String(g.id)] || 0;
          return '<td>' + (value ? fmtMinutes(value) : "–") + "</td>";
        }).join("") + "</tr>";
    }).join("");
    $("#history-table").innerHTML = head + "<tbody>" + rows + "</tbody>";

    var sHead = "<thead><tr><th>" + esc(t("history.group")) + "</th><th>" + esc(t("history.started")) +
      "</th><th>" + esc(t("history.length")) + "</th><th>" + esc(t("history.used")) + "</th><th>" +
      esc(t("history.reason")) + "</th></tr></thead>";
    var sRows = state.sessions_today.map(function (s) {
      return "<tr><td>" + esc((s.emoji ? s.emoji + " " : "") + s.group) + "</td><td>" + fmtTime(s.started_at) +
        "</td><td>" + s.planned_minutes + " " + esc(t("common.minutes")) + "</td><td>" + fmtMinutes(s.used_minutes) +
        "</td><td>" + esc(s.end_reason ? (t("history.reason." + s.end_reason) || s.end_reason) : "…") + "</td></tr>";
    }).join("");
    $("#sessions-table").innerHTML = sHead + "<tbody>" + (sRows || '<tr><td colspan="5" class="muted">' + esc(t("history.no_sessions")) + "</td></tr>") + "</tbody>";
  }

  function render() {
    if (!state) { return; }
    $("#role-badge").textContent = role === "admin" ? "admin" : "user";
    var isAdmin = role === "admin";
    $("#admin-tabs").classList.toggle("hidden", !isAdmin);
    $$("#admin-tabs .tab").forEach(function (button) {
      button.setAttribute("aria-selected", button.dataset.tab === tab ? "true" : "false");
    });
    if (!isAdmin) { tab = "today"; }
    $("#panel-groups").classList.toggle("hidden", !(isAdmin && tab === "groups"));
    $("#panel-history").classList.toggle("hidden", !(isAdmin && tab === "history"));
    $("#panel-settings").classList.toggle("hidden", !(isAdmin && tab === "settings"));
    $("#panel-today").classList.toggle("hidden", isAdmin && tab !== "today");
    renderToday();
    if (isAdmin && tab === "groups") { renderGroupList(); }
    if (isAdmin && tab === "history") { renderHistory(); }
    if (isAdmin && tab === "settings") { loadUsers(); }
    alertBox(state.adguard_error ? t("error.adguard", { detail: state.adguard_error }) : "");
  }

  // ---------------------------------------------------------------- refresh
  function refresh() {
    return api("GET", "/api/state").then(function (data) {
      setState(data);
      render();
    }).catch(function (error) {
      if (error.status === 401) { window.location.href = base + "/login"; }
    });
  }

  // ---------------------------------------------------------------- actions
  function start(groupId, minutes) {
    api("POST", "/api/session/start", { group_id: groupId, minutes: minutes })
      .then(function (data) {
        setState(data.state);
        var group = state.groups.filter(function (g) { return g.id === groupId; })[0];
        toast(t("toast.started", { name: group ? group.name : "", minutes: minutes }));
        render();
      }).catch(function (error) { toast(error.message); });
  }

  function stop(groupId) {
    api("POST", "/api/session/stop", { group_id: groupId })
      .then(function (data) {
        setState(data.state);
        var group = state.groups.filter(function (g) { return g.id === groupId; })[0];
        toast(t("toast.stopped", { name: group ? group.name : "" }));
        render();
      }).catch(function (error) { toast(error.message); });
  }

  function adminAction(action, groupId, extra) {
    var payload = { group_id: groupId };
    var path = "/api/groups/" + groupId + "/" + action;
    if (extra) { Object.keys(extra).forEach(function (k) { payload[k] = extra[k]; }); }
    if (action === "bonus") { payload.minutes = extra && extra.minutes ? extra.minutes : 15; }
    api("POST", path, payload).then(function (data) {
      if (data.state) { setState(data.state); } else { return refresh(); }
      var group = data.group || (state.groups.filter(function (g) { return g.id === groupId; })[0] || {});
      if (action === "block") { toast(t("toast.blocked", { name: group.name })); }
      if (action === "bonus") {
        toast(payload.minutes < 0
          ? t("toast.bonus_removed", { name: group.name, minutes: Math.abs(payload.minutes) })
          : t("toast.bonus", { name: group.name, minutes: payload.minutes }));
      }
      if (action === "reset") { toast(t("toast.reset", { name: group.name })); }
      if (action === "free") { toast(t("toast.free", { name: group.name, time: fmtTime(data.until) })); }
      if (action === "clearfree") { toast(t("common.saved")); }
      render();
    }).catch(function (error) { toast(error.message); });
  }

  // ------------------------------------------------------------ group editor
  function criticalDialog(hits, retry) {
    var list = hits.map(function (h) {
      return '<li><strong>' + esc(h.domain) + "</strong> — " + esc(h.note) + "</li>";
    }).join("");
    openModal(
      '<h2 class="wrap-anywhere">' + esc(t("groups.critical_title")) + "</h2>" +
      '<ul class="small mt">' + list + "</ul>" +
      '<div class="row mt"><button class="btn" data-action="modal-close">' + esc(t("common.cancel")) + "</button>" +
      '<button class="btn danger" id="critical-confirm">' + esc(t("groups.critical_confirm")) + "</button></div>"
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
      if (box) { box.textContent = error.message; box.classList.remove("hidden"); } else { toast(error.message); }
    });
  }

  function openGroupEditor(group) {
    api("GET", "/api/users")
      .then(function (data) { openGroupEditorWith(group, data.users || []); })
      .catch(function () { openGroupEditorWith(group, []); });
  }

  function openGroupEditorWith(group, users) {
    group = group || { name: "", emoji: "⏳", weekday_minutes: 60, weekend_minutes: 60, services: [], domains: [], paused: false, mode: "auto", owner_id: null };
    var selected = { services: (group.services || []).slice(), paused: !!group.paused };

    function sheet() {
      var chips = selected.services.map(function (id) {
        return '<span class="chip">' + esc(id) + ' <button type="button" data-action="remove-service" data-service="' + esc(id) + '" aria-label="remove">×</button></span>';
      }).join(" ") || '<span class="muted small">' + esc(t("groups.services_help")) + "</span>";
      return (
        '<h2>' + esc(group.id ? t("groups.edit") : t("groups.add")) + "</h2>" +
        '<p class="alert bad hidden" id="group-error" role="alert"></p>' +
        '<form id="group-form" class="stack mt">' +
        '<div class="row"><div class="w-emoji"><label for="g-emoji">' + esc(t("groups.emoji")) + '</label>' +
        '<input type="text" id="g-emoji" value="' + esc(group.emoji || "") + '" maxlength="8"></div>' +
        '<div class="grow1"><label for="g-name">' + esc(t("groups.name")) + "</label>" +
        '<input type="text" id="g-name" value="' + esc(group.name) + '" required></div></div>' +
        '<div class="row"><div class="grow1"><label for="g-weekday">' + esc(t("groups.weekday")) + "</label>" +
        '<input type="number" id="g-weekday" min="0" max="1440" value="' + group.weekday_minutes + '"></div>' +
        '<div class="grow1"><label for="g-weekend">' + esc(t("groups.weekend")) + "</label>" +
        '<input type="number" id="g-weekend" min="0" max="1440" value="' + group.weekend_minutes + '"></div></div>' +
        '<div><label>' + esc(t("groups.services")) + "</label><div class=\"row tight\" id=\"service-chips\">" + chips +
        ' <button type="button" class="btn" data-action="pick-services">+ ' + esc(t("groups.services")) + "</button></div></div>" +
        '<div><label for="g-owner">' + esc(t("groups.owner")) + "</label>" +
        '<select id="g-owner"><option value="">' + esc(t("groups.owner_shared")) + "</option>" +
        users.map(function (u) {
          var sel = String(group.owner_id) === String(u.id) ? " selected" : "";
          return '<option value="' + u.id + '"' + sel + ">" + esc(u.username) + " (" + esc(u.role) + ")</option>";
        }).join("") + "</select></div>" +
        '<div><label for="g-mode">' + esc(t("groups.mode")) + "</label>" +
        '<select id="g-mode"><option value="auto"' + ((group.mode || "auto") === "auto" ? " selected" : "") + ">" + esc(t("groups.mode.auto")) + "</option>" +
        '<option value="session"' + (group.mode === "session" ? " selected" : "") + ">" + esc(t("groups.mode.session")) + "</option></select>" +
        '<p class="sub">' + esc(t("groups.mode_help")) + "</p></div>" +
        '<div><label for="g-domains">' + esc(t("groups.domains")) + "</label>" +
        '<textarea id="g-domains" placeholder="example.com">' + esc((group.domains || []).join("\n")) + "</textarea>" +
        '<p class="sub">' + esc(t("groups.domains_help")) + "</p></div>" +
        '<label class="row tight"><input type="checkbox" id="g-paused"' + (selected.paused ? " checked" : "") + "> " + esc(t("groups.paused")) + "</label>" +
        '<div class="row"><button class="btn primary" type="submit">' + esc(t("common.save")) + "</button>" +
        '<button class="btn" type="button" data-action="modal-close">' + esc(t("common.cancel")) + "</button>" +
        (group.id ? '<button class="btn danger" type="button" data-action="delete-group" data-group="' + group.id + '">' + esc(t("common.delete")) + "</button>" : "") +
        (group.id ? '<button class="btn ghost" type="button" data-action="test-group" data-group="' + group.id + '">' + esc(t("groups.test")) + "</button>" : "") +
        "</div></form>"
      );
    }

    function paint() { openModal(sheet()); bind(); }

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
          paused: $("#g-paused").checked,
          mode: $("#g-mode").value,
          owner_id: $("#g-owner").value === "" ? null : Number($("#g-owner").value)
        };
        saveGroup(payload, group.id);
      });
      $$("[data-action='pick-services']").forEach(function (button) {
        button.addEventListener("click", function () { openServicePicker(selected.services, function (next) { selected.services = next; paint(); }); });
      });
      $$("[data-action='remove-service']").forEach(function (button) {
        button.addEventListener("click", function () {
          selected.services = selected.services.filter(function (id) { return id !== button.dataset.service; });
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
        '<div class="search mt"><input type="search" id="service-search" placeholder="' + esc(t("groups.services")) + '" aria-label="search"></div>' +
        '<div class="list mt" id="service-list" class="scroll-y"></div>' +
        '<div class="row mt"><button class="btn primary" id="service-done">' + esc(t("common.save")) + "</button>" +
        '<button class="btn" data-action="modal-close">' + esc(t("common.cancel")) + "</button></div>"
      );
    }
    openModal(sheet());
    var selected = current.slice();
    var all = [];

    function paint(list) {
      $("#service-list").innerHTML = list.map(function (service) {
        var checked = selected.indexOf(service.id) >= 0;
        var icon = service.icon_svg ? '<img alt="" width="20" height="20" src="data:image/svg+xml;base64,' + service.icon_svg + '">' : "";
        return '<label class="item"><input type="checkbox" data-service="' + esc(service.id) + '"' + (checked ? " checked" : "") +
          (service.group ? " disabled" : "") + "> " + icon + ' <span class="grow">' + esc(service.name) +
          (service.group ? ' <span class="muted small">(' + esc(service.group) + ")</span>" : "") + "</span></label>";
      }).join("") || '<p class="muted small">…</p>';
      $$("#service-list input[type=checkbox]").forEach(function (box) {
        box.addEventListener("change", function () {
          var id = box.dataset.service;
          if (box.checked) { if (selected.indexOf(id) < 0) { selected.push(id); } }
          else { selected = selected.filter(function (s) { return s !== id; }); }
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
      paint(all.filter(function (s) { return s.id.indexOf(q) >= 0 || s.name.toLowerCase().indexOf(q) >= 0; }));
    });

    $("#service-done").addEventListener("click", function () { closeModal(); done(selected); });
    load();
  }

  function resetGroup(groupId, name) {
    openModal(
      '<h2 class="wrap-anywhere">' + esc(t("admin.reset_today")) + " — " + esc(name) + "</h2>" +
      '<p class="sub">' + esc(t("admin.reset_confirm", { name: name })) + "</p>" +
      '<div class="row mt"><button class="btn" data-action="modal-close">' + esc(t("common.cancel")) + "</button>" +
      '<button class="btn danger" id="reset-confirm">' + esc(t("admin.reset_today")) + "</button></div>"
    );
    $("#reset-confirm").addEventListener("click", function () {
      api("POST", "/api/groups/" + groupId + "/reset", {}).then(function (data) {
        closeModal();
        toast(t("toast.reset", { name: data.group.name }));
        refresh();
      }).catch(function (error) { toast(error.message); });
    });
  }

  function deleteGroup(groupId, name) {
    openModal(
      '<h2>' + esc(t("common.delete")) + " — " + esc(name) + "</h2>" +
      '<p class="sub">' + esc(t("groups.delete_warning")) + "</p>" +
      '<div class="row mt"><button class="btn" data-action="modal-close">' + esc(t("common.cancel")) + "</button>" +
      '<button class="btn danger" id="delete-confirm">' + esc(t("common.delete")) + "</button></div>"
    );
    $("#delete-confirm").addEventListener("click", function () {
      api("DELETE", "/api/groups/" + groupId).then(function () {
        closeModal();
        toast(t("toast.deleted", { name: name }));
        refresh();
      }).catch(function (error) { toast(error.message); });
    });
  }

  function testGroup(groupId) {
    closeModal();
    api("GET", "/api/groups/" + groupId + "/test").then(function (data) {
      var rows = data.results.map(function (r) {
        return "<tr><td>" + esc(r.host) + '</td><td><span class="pill ' + (r.blocked ? "bad" : "ok") + '">' +
          (r.blocked ? "blocked" : "allowed") + "</span></td></tr>";
      }).join("");
      openModal(
        '<h2>' + esc(t("groups.test")) + "</h2>" +
        '<div class="table-wrap mt"><table><thead><tr><th>host</th><th>AdGuard</th></tr></thead><tbody>' + rows + "</tbody></table></div>" +
        '<div class="row mt"><button class="btn" data-action="modal-close">' + esc(t("common.close")) + "</button></div>"
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
        ' <span class="pill ' + (isAdmin ? "ok" : "muted") + '">' + esc(t("users.role." + u.role)) + "</span>" +
        (u.disabled ? ' <span class="pill bad">' + esc(t("users.disabled")) + "</span>" : "") + "</div>" +
        '<div class="muted small">' + (u.last_login ? "last login " + fmtTime(u.last_login) : "never signed in") + "</div></div>" +
        '<div class="row tight">' +
        '<button class="btn ghost" data-action="user-password" data-user="' + u.id + '" data-username="' + esc(u.username) + '">' + esc(t("users.reset")) + "</button>" +
        '<button class="btn ghost" data-action="user-role" data-user="' + u.id + '" data-role="' + (isAdmin ? "user" : "admin") + '">' + esc(isAdmin ? t("users.role.user") : t("users.role.admin")) + "</button>" +
        '<button class="btn ghost" data-action="user-toggle" data-user="' + u.id + '" data-disabled="' + (u.disabled ? "0" : "1") + '">' + esc(u.disabled ? t("users.enable") : t("users.disable")) + "</button>" +
        '<button class="btn danger" data-action="user-delete" data-user="' + u.id + '" data-username="' + esc(u.username) + '">' + esc(t("users.delete")) + "</button>" +
        "</div></div>";
    }).join("");
  }

  function loadUsers() {
    return api("GET", "/api/users").then(function (data) {
      state.users = data.users || [];
      renderUsers();
    }).catch(function (error) { toast(error.message); });
  }

  // --------------------------------------------------------------- discover
  function renderDiscover(data) {
    discoverData = data;
    var select = $("#discover-client");
    var current = select.value;
    select.innerHTML = '<option value="">' + esc(t("discover.all_clients")) + "</option>" +
      data.clients.map(function (c) {
        return '<option value="' + esc(c.client) + '">' + esc(c.client) +  + "</option>";
      }).join("");
    select.value = current;

    if (!data.domains.length) {
      $("#discover-results").innerHTML = '<p class="alert">' + esc(t("discover.none")) + "<br>" + esc(t("discover.empty_hint")) + "</p>";
      return;
    }
    var rows = data.domains.map(function (d) {
      return '<tr><td><input type="checkbox" data-domain="' + esc(d.domain) + '" aria-label="' + esc(d.domain) + '"></td>' +
        "<td>" + esc(d.domain) + "</td><td>" + d.count + "</td><td>" + esc(t("discover.last_seen", { ago: d.last_seen_ago })) + "</td></tr>";
    }).join("");
    $("#discover-results").innerHTML =
      '<div class="table-wrap"><table><thead><tr><th></th><th>' + esc(t("discover.domain")) + "</th><th>" +
      esc(t("discover.queries")) + "</th><th>" + esc(t("discover.seen")) + "</th></tr></thead><tbody>" + rows + "</tbody></table></div>" +
      '<div class="row mt"><select id="discover-target" aria-label="' + esc(t("discover.add_to")) + '">' +
      state.groups.map(function (g) { return '<option value="' + g.id + '">' + esc((g.emoji ? g.emoji + " " : "") + g.name) + "</option>"; }).join("") +
      '</select><button class="btn primary" id="discover-add">' + esc(t("discover.add")) + "</button></div>";
    $("#discover-add").addEventListener("click", function () {
      var checked = $$("#discover-results input[type=checkbox]:checked").map(function (box) { return box.dataset.domain; });
      if (!checked.length) { return; }
      var groupId = Number($("#discover-target").value);
      api("POST", "/api/discover/add", { group_id: groupId, domains: checked }).then(function (data) {
        toast(t("toast.domains_added", { count: data.added.length, name: data.group.name }));
        refresh();
        runDiscover();
      }).catch(function (error) {
        if (error.status === 409 && error.payload && error.payload.critical) {
          criticalDialog(error.payload.critical, function () {
            api("POST", "/api/discover/add", { group_id: groupId, domains: checked, confirm_critical: true })
              .then(function () { toast(t("common.saved")); refresh(); })
              .catch(function (e) { toast(e.message); });
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
    api("GET", "/api/discover?minutes=" + minutes + "&client=" + encodeURIComponent(client) + "&show_all=" + (showAll ? "true" : "false"))
      .then(renderDiscover)
      .catch(function (error) { $("#discover-results").innerHTML = '<p class="alert bad">' + esc(error.message) + "</p>"; });
  }

  // ---------------------------------------------------------------- wiring
  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-action]");
    if (!target) { return; }
    var action = target.dataset.action;
    var groupId = target.dataset.group ? Number(target.dataset.group) : null;

    if (action === "start") { start(groupId, Number(target.dataset.minutes)); }
    else if (action === "stop") { stop(groupId); }
    else if (action === "block") { adminAction("block", groupId); }
    else if (action === "bonus") { adminAction("bonus", groupId, { minutes: Number(target.dataset.minutes || 15) }); }
    else if (action === "bonus-custom") {
      var input = document.querySelector('[data-custom-bonus="' + groupId + '"]');
      var value = input ? Number(input.value) : 0;
      if (!value) { toast(t("admin.custom_placeholder")); return; }
      adminAction("bonus", groupId, { minutes: value });
    }
    else if (action === "reset") {
      var g = state.groups.filter(function (x) { return x.id === groupId; })[0];
      resetGroup(groupId, g ? g.name : "");
    }
    else if (action === "free") {
      var input = document.querySelector('[data-free-time="' + groupId + '"]');
      adminAction("free", groupId, { until: input ? input.value : "21:00" });
    }
    else if (action === "clearfree") { adminAction("free/clear", groupId); }
    else if (action === "unblock") { adminAction("force/clear", groupId); }
    else if (action === "user-password") { resetUserPassword(Number(target.dataset.user), target.dataset.username); }
    else if (action === "user-role") {
      api("POST", "/api/users/" + target.dataset.user + "/role", { role: target.dataset.role })
        .then(function () { toast(t("common.saved")); loadUsers(); })
        .catch(function (error) { toast(error.message); });
    }
    else if (action === "user-toggle") {
      api("POST", "/api/users/" + target.dataset.user + "/disabled", { disabled: target.dataset.disabled === "1" })
        .then(function () { toast(t("common.saved")); loadUsers(); })
        .catch(function (error) { toast(error.message); });
    }
    else if (action === "user-delete") { deleteUser(Number(target.dataset.user), target.dataset.username); }
    else if (action === "edit") {
      var group = state.groups.filter(function (g) { return g.id === groupId; })[0];
      if (group) { openGroupEditor(group); }
    }
    else if (action === "pause") {
      api("POST", "/api/groups/" + groupId + "/pause", { paused: target.dataset.paused === "1" })
        .then(function (data) { toast(t(data.group.paused ? "toast.paused" : "toast.resumed", { name: data.group.name })); refresh(); })
        .catch(function (error) { toast(error.message); });
    }
    else if (action === "test-group") { testGroup(groupId); }
    else if (action === "delete-group") {
      var g = state.groups.filter(function (x) { return x.id === groupId; })[0];
      deleteGroup(groupId, g ? g.name : "");
    }
    else if (action === "modal-close") { closeModal(); }
  });

  $$("#admin-tabs .tab").forEach(function (button) {
    button.addEventListener("click", function () { tab = button.dataset.tab; render(); });
  });

  var newGroupButton = $("#new-group");
  if (newGroupButton) { newGroupButton.addEventListener("click", function () { openGroupEditor(null); }); }

  var discoverButton = $("#discover-run");
  if (discoverButton) { discoverButton.addEventListener("click", runDiscover); }

  var logoutButton = $("#logout");
  if (logoutButton) {
    logoutButton.addEventListener("click", function () {
      api("POST", "/api/logout", {}).then(function () { window.location.href = base + "/login"; });
    });
  }

  var pinForm = $("#pin-form");
  if (pinForm) {
    pinForm.addEventListener("submit", function (event) {
      event.preventDefault();
      api("POST", "/api/admin/credentials", { role: "user", secret: $("#new-pin").value })
        .then(function () { $("#new-pin").value = ""; toast(t("toast.pin")); })
        .catch(function (error) { toast(error.message); });
    });
  }

  var passwordForm = $("#password-form");
  if (passwordForm) {
    passwordForm.addEventListener("submit", function (event) {
      event.preventDefault();
      api("POST", "/api/admin/credentials", { role: "admin", secret: $("#new-password").value })
        .then(function () { $("#new-password").value = ""; toast(t("toast.password")); })
        .catch(function (error) { toast(error.message); });
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
      '<input type="password" id="user-password" autocomplete="new-password" placeholder="' + esc(t("users.password")) + '" aria-label="' + esc(t("users.password")) + '">' +
      '<p class="sub">' + esc(t("users.password_hint")) + "</p>" +
      '<div class="row"><button class="btn primary" type="submit">' + esc(t("common.save")) + "</button>" +
      '<button class="btn" type="button" data-action="modal-close">' + esc(t("common.cancel")) + "</button></div></form>"
    );
    $("#user-password-form").addEventListener("submit", function (event) {
      event.preventDefault();
      api("POST", "/api/users/" + userId + "/password", { password: $("#user-password").value })
        .then(function () { closeModal(); toast(t("toast.user_password", { name: username })); })
        .catch(function (error) { toast(error.message); });
    });
  }

  function deleteUser(userId, username) {
    openModal(
      '<h2 class="wrap-anywhere">' + esc(t("users.delete")) + " — " + esc(username) + "</h2>" +
      '<p class="sub">' + esc(t("common.confirm")) + "</p>" +
      '<div class="row mt"><button class="btn" data-action="modal-close">' + esc(t("common.cancel")) + "</button>" +
      '<button class="btn danger" id="user-delete-confirm">' + esc(t("common.delete")) + "</button></div>"
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

  // 1s countdown for active sessions
  window.setInterval(function () {
    if (!state) { return; }
    var now = Math.floor(Date.now() / 1000);
    $$("[data-status][data-ends]").forEach(function (el) {
      var left = Number(el.dataset.ends) - now;
      if (left <= 0) { refresh(); return; }
      el.textContent = (el.dataset.prefix || "") + fmtClock(left) + " " + t("status.left_suffix");
    });
  }, 1000);

  // 5s polling
  window.setInterval(function () { if (state) { refresh(); } }, 5000);

  // boot
  fetch(base + "/api/i18n", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (data) { i18n = data || {}; })
    .catch(function () { i18n = {}; })
    .then(function () {
      return fetch(base + "/api/meta", { credentials: "same-origin" }).then(function (r) { return r.json(); });
    })
    .then(function (meta) {
      if (!meta.setup_complete) { window.location.href = base + "/login"; return null; }
      if (!meta.role) { window.location.href = base + "/login"; return null; }
      role = meta.role;
      return refresh();
    })
    .catch(function () { window.location.href = base + "/login"; });
})();
