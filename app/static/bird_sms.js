(function () {
  "use strict";

  const csrf = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrf ? csrf.getAttribute("content") : "";

  async function api(path, options) {
    const opts = Object.assign({ method: "GET" }, options || {});
    opts.headers = Object.assign(
      { "X-CSRFToken": csrfToken },
      opts.body instanceof FormData ? {} : { "Content-Type": "application/json" },
      opts.headers || {}
    );
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      let message = (data && (data.error || data.message)) || res.statusText;
      if (Array.isArray(message)) {
        message = message.map((item) => item.msg || JSON.stringify(item)).join("; ");
      }
      throw new Error(message);
    }
    return data;
  }

  function status(el, text, bad) {
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("error", !!bad);
  }

  function formNumber(form, name) {
    const value = form.elements[name] ? form.elements[name].value : "";
    return value === "" ? undefined : Number(value);
  }

  function formString(form, name) {
    return form.elements[name] ? form.elements[name].value.trim() : "";
  }

  function checked(form, name) {
    return !!(form.elements[name] && form.elements[name].checked);
  }

  function removeUndefined(obj) {
    Object.keys(obj).forEach((key) => obj[key] === undefined && delete obj[key]);
    return obj;
  }

  function initContacts() {
    const panel = document.getElementById("bird-import-panel");
    const toggle = document.getElementById("bird-toggle-import");
    if (toggle && panel) toggle.addEventListener("click", () => { panel.hidden = !panel.hidden; });

    const addBtn = document.getElementById("bird-add-contact-btn");
    if (addBtn) {
      addBtn.addEventListener("click", async () => {
        const out = document.getElementById("bird-add-status");
        try {
          await api("/api/bird-sms/contacts", {
            method: "POST",
            body: JSON.stringify({
              phone: document.getElementById("bird-new-phone").value,
              first_name: document.getElementById("bird-new-first").value || undefined,
              last_name: document.getElementById("bird-new-last").value || undefined,
            }),
          });
          status(out, "Contact added. Reloading...", false);
          window.location.reload();
        } catch (err) { status(out, err.message, true); }
      });
    }

    const fileInput = document.getElementById("bird-import-file");
    const previewBtn = document.getElementById("bird-preview-btn");
    let mappingData = null;
    if (previewBtn && fileInput) {
      previewBtn.addEventListener("click", async () => {
        const out = document.getElementById("bird-import-status");
        if (!fileInput.files.length) { status(out, "Choose a file first.", true); return; }
        const body = new FormData();
        body.append("file", fileInput.files[0]);
        try {
          status(out, "Reading...", false);
          mappingData = await api("/api/bird-sms/contacts/import/preview", { method: "POST", body: body });
          const section = document.getElementById("bird-mapping-section");
          section.hidden = false;
          ["phone", "first_name", "last_name", "country"].forEach((field) => {
            const select = document.getElementById("bird-map-" + field);
            select.innerHTML = "";
            const blank = document.createElement("option");
            blank.value = "";
            blank.textContent = field === "phone" ? "Select column" : "Ignore";
            select.appendChild(blank);
            mappingData.columns.forEach((column) => {
              const option = document.createElement("option");
              option.value = column;
              option.textContent = column;
              if (mappingData.suggested_mapping[field] === column) option.selected = true;
              select.appendChild(option);
            });
          });
          document.getElementById("bird-preview-count").textContent =
            mappingData.total_rows + " row(s) found" + (mappingData.truncated ? " (preview truncated)" : "");
          status(out, "File read. Map the phone column and import.", false);
        } catch (err) { status(out, err.message, true); }
      });
    }

    const importBtn = document.getElementById("bird-import-btn");
    if (importBtn && fileInput) {
      importBtn.addEventListener("click", async () => {
        const out = document.getElementById("bird-import-result");
        if (!fileInput.files.length) { status(out, "Choose a file first.", true); return; }
        const mapping = {
          phone: document.getElementById("bird-map-phone").value || undefined,
          first_name: document.getElementById("bird-map-first_name").value || undefined,
          last_name: document.getElementById("bird-map-last_name").value || undefined,
          country: document.getElementById("bird-map-country").value || undefined,
        };
        if (!mapping.phone) { status(out, "Map the phone column first.", true); return; }
        const body = new FormData();
        body.append("file", fileInput.files[0]);
        body.append("mapping", JSON.stringify(removeUndefined(mapping)));
        try {
          status(out, "Importing...", false);
          const result = await api("/api/bird-sms/contacts/import", { method: "POST", body: body });
          status(out, "Imported " + (result.imported || 0) + ", duplicates " + (result.skipped_duplicates || 0) + ", invalid " + ((result.invalid || []).length) + ". Reloading...", false);
          window.location.reload();
        } catch (err) { status(out, err.message, true); }
      });
    }

    document.querySelectorAll("[data-bird-save-contact]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const row = btn.closest("tr");
        const data = {};
        row.querySelectorAll("input[data-field]").forEach((input) => { data[input.dataset.field] = input.value; });
        try {
          await api("/api/bird-sms/contacts/" + encodeURIComponent(btn.dataset.birdSaveContact), {
            method: "PATCH", body: JSON.stringify(data),
          });
          status(btn, "Saved", false);
        } catch (err) { alert(err.message); }
      });
    });

    document.querySelectorAll("[data-bird-delete-contact]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Delete this Bird SMS contact?")) return;
        try {
          await api("/api/bird-sms/contacts/" + encodeURIComponent(btn.dataset.birdDeleteContact), { method: "DELETE" });
          btn.closest("tr").remove();
        } catch (err) { alert(err.message); }
      });
    });

    document.querySelectorAll("[data-bird-restore-contact]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api("/api/bird-sms/contacts/restore", { method: "POST", body: JSON.stringify({ ids: [btn.dataset.birdRestoreContact] }) });
          btn.closest("tr").remove();
        } catch (err) { alert(err.message); }
      });
    });

    const listForm = document.getElementById("bird-list-create-form");
    if (listForm) {
      listForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const out = document.getElementById("bird-list-status");
        try {
          await api("/api/bird-sms/lists", {
            method: "POST",
            body: JSON.stringify({
              name: formString(listForm, "name"),
              description: formString(listForm, "description") || undefined,
              phones: formString(listForm, "phones").split(/[\s,;]+/).filter(Boolean),
            }),
          });
          window.location.reload();
        } catch (err) { status(out, err.message, true); }
      });
    }

    document.querySelectorAll("[data-bird-delete-list]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Delete this list? Contacts are kept.")) return;
        try {
          await api("/api/bird-sms/lists/" + encodeURIComponent(btn.dataset.birdDeleteList), { method: "DELETE" });
          btn.closest(".list-card").remove();
        } catch (err) { alert(err.message); }
      });
    });
  }

  function initTemplates() {
    const dataEl = document.getElementById("bird-templates-data");
    if (!dataEl) return;
    const templates = JSON.parse(dataEl.textContent || "[]");
    const byId = {};
    templates.forEach((t) => { byId[t.id] = t; });
    const form = document.getElementById("bird-template-form");
    if (!form) return;
    const title = document.getElementById("bird-template-editor-title");

    function reset() {
      form.reset();
      form.elements.id.value = "";
      title.textContent = "New template";
      document.getElementById("bird-template-preview").hidden = true;
    }

    document.querySelectorAll("[data-bird-template-edit]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const item = byId[btn.dataset.birdTemplateEdit];
        if (!item) return;
        form.elements.id.value = item.id;
        form.elements.name.value = item.name || "";
        form.elements.category.value = item.category || "transactional";
        form.elements.content.value = item.content || "";
        form.elements.include_stop_code.checked = !!item.include_stop_code;
        title.textContent = "Edit template";
        form.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });

    document.querySelectorAll("[data-bird-template-delete]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Delete this template?")) return;
        try {
          await api("/api/bird-sms/templates/" + encodeURIComponent(btn.dataset.birdTemplateDelete), { method: "DELETE" });
          window.location.reload();
        } catch (err) { alert(err.message); }
      });
    });

    const newBtn = document.getElementById("bird-template-new-btn");
    if (newBtn) newBtn.addEventListener("click", reset);
    const resetBtn = document.getElementById("bird-template-reset-btn");
    if (resetBtn) resetBtn.addEventListener("click", reset);

    const previewBtn = document.getElementById("bird-template-preview-btn");
    if (previewBtn) {
      previewBtn.addEventListener("click", async () => {
        const out = document.getElementById("bird-template-status");
        try {
          const data = await api("/api/bird-sms/templates/preview", {
            method: "POST",
            body: JSON.stringify({
              content: form.elements.content.value,
              include_stop_code: form.elements.include_stop_code.checked,
              smart_encoding: true,
            }),
          });
          document.getElementById("bird-template-preview").hidden = false;
          document.getElementById("bird-template-preview-body").textContent = data.content;
          document.getElementById("bird-template-preview-meta").textContent =
            data.segments + " segment(s) / " + data.encoding;
          status(out, "", false);
        } catch (err) { status(out, err.message, true); }
      });
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const out = document.getElementById("bird-template-status");
      const id = form.elements.id.value;
      const payload = {
        name: form.elements.name.value.trim(),
        content: form.elements.content.value,
        category: form.elements.category.value,
        include_stop_code: form.elements.include_stop_code.checked,
      };
      try {
        await api("/api/bird-sms/templates" + (id ? "/" + encodeURIComponent(id) : ""), {
          method: id ? "PATCH" : "POST", body: JSON.stringify(payload),
        });
        window.location.reload();
      } catch (err) { status(out, err.message, true); }
    });

    const refresh = document.getElementById("bird-catalog-refresh");
    if (refresh) {
      refresh.addEventListener("click", async () => {
        try {
          const data = await api("/api/bird-sms/templates/catalog?refresh=1");
          alert(data.error ? data.error : "Catalogue refreshed: " + data.items.length + " item(s). Reload to see it.");
        } catch (err) { alert(err.message); }
      });
    }
  }

  function initSettings() {
    const form = document.getElementById("bird-settings-form");
    if (!form) return;

    function payload() {
      const data = {};
      form.querySelectorAll("input, select").forEach((el) => {
        if (!el.name || el.disabled) return;
        if (el.type === "password") {
          if (el.value.trim()) data[el.name] = el.value.trim();
          return;
        }
        if (el.type === "checkbox") { data[el.name] = el.checked; return; }
        if (el.type === "number") {
          if (el.value !== "") data[el.name] = Number(el.value);
          return;
        }
        data[el.name] = el.value;
      });
      if (!data.telegram_chat_id) delete data.telegram_chat_id;
      if (!data.from_number) delete data.from_number;
      if (!data.default_country_code) delete data.default_country_code;
      if (!data.warmup_start_date) delete data.warmup_start_date;
      if (!data.default_window_start) delete data.default_window_start;
      if (!data.default_window_end) delete data.default_window_end;
      return data;
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const out = document.getElementById("bird-settings-status");
      try {
        await api("/api/bird-sms/settings", { method: "PATCH", body: JSON.stringify(payload()) });
        status(out, "Saved.", false);
        window.setTimeout(() => window.location.reload(), 500);
      } catch (err) { status(out, err.message, true); }
    });

    const testBtn = document.getElementById("bird-telegram-test-btn");
    if (testBtn) {
      testBtn.addEventListener("click", async () => {
        const out = document.getElementById("bird-telegram-status");
        try {
          await api("/api/settings/test-telegram", { method: "POST", body: JSON.stringify({ section: "bird_sms" }) });
          status(out, "Test sent - check the chat.", false);
        } catch (err) { status(out, err.message, true); }
      });
    }

    const clearBtn = document.getElementById("bird-telegram-clear-btn");
    if (clearBtn) {
      clearBtn.addEventListener("click", async () => {
        const out = document.getElementById("bird-telegram-status");
        if (!confirm("Clear the Bird SMS Telegram overrides? Global settings will be used.")) return;
        try {
          await api("/api/bird-sms/settings", {
            method: "PATCH",
            body: JSON.stringify({ telegram_bot_token: "", telegram_chat_id: "" }),
          });
          status(out, "Overrides cleared.", false);
          window.setTimeout(() => window.location.reload(), 500);
        } catch (err) { status(out, err.message, true); }
      });
    }
  }

  function initCampaignForm() {
    const form = document.getElementById("bird-campaign-form");
    if (!form) return;
    const campaignId = form.dataset.campaignId || "";
    const mode = document.getElementById("bird-message-mode");

    function syncMode() {
      const template = mode && mode.value === "template";
      document.getElementById("bird-text-fields").hidden = template;
      document.getElementById("bird-template-fields").hidden = !template;
    }
    if (mode) { mode.addEventListener("change", syncMode); syncMode(); }

    let templateParams = null;
    try {
      const raw = form.elements.template_parameters ? form.elements.template_parameters.value : "";
      templateParams = raw ? JSON.parse(raw) : null;
    } catch (e) { templateParams = null; }
    if (templateParams && form.elements.template_parameters) {
      form.elements.template_parameters.value = JSON.stringify(templateParams, null, 2);
    }

    const previewBtn = document.getElementById("bird-campaign-preview-btn");
    if (previewBtn) {
      previewBtn.addEventListener("click", async () => {
        const out = document.getElementById("bird-campaign-preview-meta");
        try {
          const data = await api("/api/bird-sms/templates/preview", {
            method: "POST",
            body: JSON.stringify({
              content: form.elements.text.value,
              include_stop_code: form.elements.include_stop_code.checked,
              smart_encoding: form.elements.smart_encoding.checked,
            }),
          });
          document.getElementById("bird-campaign-preview").hidden = false;
          document.getElementById("bird-campaign-preview-body").textContent = data.content;
          out.textContent = data.segments + " segment(s) / " + data.encoding;
        } catch (err) { status(out, err.message, true); }
      });
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const out = document.getElementById("bird-campaign-status");
      const btn = document.getElementById("bird-campaign-save-btn");
      btn.disabled = true;
      try {
        let parameters;
        const rawParameters = form.elements.template_parameters.value.trim();
        if (rawParameters) parameters = JSON.parse(rawParameters);
        const data = removeUndefined({
          name: form.elements.name.value.trim(),
          from_number: form.elements.from_number.value.trim() || undefined,
          priority: form.elements.priority.value,
          category: form.elements.category.value,
          message_mode: form.elements.message_mode.value,
          text: form.elements.message_mode.value === "text" ? form.elements.text.value : undefined,
          include_stop_code: form.elements.include_stop_code.checked,
          smart_encoding: form.elements.smart_encoding.checked,
          template_scope: form.elements.template_scope.value || undefined,
          template_slug: form.elements.template_slug.value.trim() || undefined,
          template_language: form.elements.template_language.value.trim() || undefined,
          template_name: form.elements.template_name.value.trim() || undefined,
          template_category: form.elements.template_category.value || undefined,
          template_parameters: parameters,
          batch_size: formNumber(form, "batch_size"),
          send_rate_per_minute: formNumber(form, "send_rate_per_minute"),
          stagger_seconds: formNumber(form, "stagger_seconds"),
          schedule_at: formString(form, "schedule_at") || undefined,
          window_start: formString(form, "window_start") || undefined,
          window_end: formString(form, "window_end") || undefined,
          window_timezone: formString(form, "window_timezone") || undefined,
          soft_bounce_policy: form.elements.soft_bounce_policy.value,
          soft_bounce_threshold: formNumber(form, "soft_bounce_threshold"),
        });
        const listIds = Array.from(form.querySelectorAll('input[name="list_ids"]:checked')).map((el) => el.value);
        const phones = formString(form, "recipient_phones").split(/[\s,;]+/).filter(Boolean);

        if (campaignId) {
          await api("/api/bird-sms/campaigns/" + encodeURIComponent(campaignId), { method: "PATCH", body: JSON.stringify(data) });
          if (listIds.length) {
            await api("/api/bird-sms/campaigns/" + encodeURIComponent(campaignId) + "/lists", {
              method: "POST", body: JSON.stringify({ list_ids: listIds, attach: true }),
            });
          }
          if (phones.length) {
            await api("/api/bird-sms/campaigns/" + encodeURIComponent(campaignId) + "/recipients", {
              method: "POST", body: JSON.stringify({ phones: phones }),
            });
          }
          window.location.href = "/bird-sms/campaigns/" + encodeURIComponent(campaignId);
        } else {
          data.list_ids = listIds;
          data.recipient_phones = phones;
          const result = await api("/api/bird-sms/campaigns", { method: "POST", body: JSON.stringify(data) });
          window.location.href = "/bird-sms/campaigns/" + encodeURIComponent(result.campaign.id);
        }
      } catch (err) {
        status(out, err.message, true);
        btn.disabled = false;
      }
    });
  }

  function initCampaignDetail() {
    const actions = document.querySelector("[data-bird-campaign-actions]");
    if (!actions) return;
    const campaignId = actions.dataset.campaignId;
    const out = document.getElementById("bird-action-status");

    actions.querySelectorAll("[data-bird-action]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const action = btn.dataset.birdAction;
        if (action === "cancel" && !confirm("Cancel this campaign?")) return;
        btn.disabled = true;
        try {
          await api("/api/bird-sms/campaigns/" + encodeURIComponent(campaignId) + "/action", {
            method: "POST", body: JSON.stringify({ action: action }),
          });
          status(out, "Campaign " + action + ". Reloading...", false);
          window.location.reload();
        } catch (err) {
          status(out, err.message, true);
          btn.disabled = false;
        }
      });
    });

    const testBtn = document.getElementById("bird-send-test-btn");
    if (testBtn) {
      testBtn.addEventListener("click", async () => {
        const to = prompt("Send one real Bird SMS to (E.164, e.g. +15551234567):");
        if (!to) return;
        try {
          const data = await api("/api/bird-sms/campaigns/" + encodeURIComponent(campaignId) + "/send-test", {
            method: "POST", body: JSON.stringify({ to: to }),
          });
          status(out, "Test sent to " + data.to + ". Bird id " + data.message_id, false);
        } catch (err) { status(out, err.message, true); }
      });
    }

    document.querySelectorAll("[data-bird-clone]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const cloneStatus = document.getElementById("bird-clone-status");
        try {
          const data = await api("/api/bird-sms/campaigns/" + encodeURIComponent(campaignId) + "/clone", {
            method: "POST", body: JSON.stringify({ mode: btn.dataset.birdClone }),
          });
          window.location.href = "/bird-sms/campaigns/" + encodeURIComponent(data.campaign.id);
        } catch (err) { status(cloneStatus, err.message, true); }
      });
    });
  }

  initContacts();
  initTemplates();
  initSettings();
  initCampaignForm();
  initCampaignDetail();
})();
