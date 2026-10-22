// SMS workspace behaviour. Mutations go through the validated /api/sms/* JSON
// endpoints; this file only collects form state and renders the responses.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const csrf = () => {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta && meta.content ? meta.content : "";
  };

  async function api(url, body, method) {
    const res = await fetch(url, {
      method: method || "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify(body === undefined ? {} : body),
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* empty */ }
    if (!res.ok) {
      const parts = [];
      if (data.error) parts.push(typeof data.error === "string" ? data.error : JSON.stringify(data.error));
      if (Array.isArray(data.reasons)) parts.push(data.reasons.join(" "));
      throw new Error(parts.join(" ") || res.statusText);
    }
    return data;
  }

  async function upload(url, formData) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "X-CSRFToken": csrf() },
      body: formData,
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* empty */ }
    if (!res.ok) {
      throw new Error(typeof data.error === "string" ? data.error : (res.statusText || "upload failed"));
    }
    return data;
  }

  function status(id, text, isError) {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    el.style.color = isError ? "var(--danger)" : "var(--muted)";
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function splitList(value) {
    return String(value || "")
      .split(/[\n,;]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function formValue(form, name) {
    const field = form.elements[name];
    if (!field) return undefined;
    if (field.type === "checkbox") return Boolean(field.checked);
    const value = String(field.value || "").trim();
    return value === "" ? undefined : value;
  }

  function numberValue(form, name) {
    const raw = formValue(form, name);
    if (raw === undefined) return undefined;
    const n = Number(raw);
    return Number.isFinite(n) ? n : undefined;
  }

  // ------------------------------------------------------------------ //
  // Credits
  // ------------------------------------------------------------------ //
  function renderCredits(account) {
    const value = $("sms-credit-value");
    if (value) value.textContent = account && account.credits != null ? account.credits : "—";
    const note = $("sms-credit-note");
    if (note) {
      note.textContent = account && account.available
        ? (account.credits_type || "SMS credits") + " · fetched from Brevo" +
          (account.fetched_at ? " at " + String(account.fetched_at).slice(11, 16) + " UTC" : "")
        : "Balance unavailable (check the Brevo API key) — refresh to retry.";
    }
    const low = $("sms-credit-low");
    if (low) low.hidden = !(account && account.low);
  }

  async function loadCredits(force) {
    const value = $("sms-credit-value");
    if (!value) return;
    const button = $("sms-credits-refresh");
    if (button) button.disabled = true;
    try {
      const res = await fetch("/api/sms/account" + (force ? "?refresh=1" : ""));
      const data = await res.json();
      renderCredits(data.account || {});
    } catch (_) {
      renderCredits({ available: false, credits: null, low: false });
    } finally {
      if (button) button.disabled = false;
    }
  }

  function initCredits() {
    const button = $("sms-credits-refresh");
    if (button) button.addEventListener("click", () => loadCredits(true));
  }

  function initSeeMore() {
    const button = document.querySelector("[data-sms-see-more]");
    if (!button) return;
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-see-more]").forEach((row) => { row.hidden = false; });
      button.parentElement.hidden = true;
    });
  }

  // ------------------------------------------------------------------ //
  // Contacts: import, inline edit, bulk actions and lists
  // ------------------------------------------------------------------ //
  const IMPORT_FIELDS = ["phone", "first_name", "last_name", "country"];
  let importState = null;

  function mappingFor(field) {
    return (importState && importState.mapping[field]) || "";
  }

  function renderMapping() {
    const columns = importState.columns;
    IMPORT_FIELDS.forEach((field) => {
      const select = $("sms-map-" + field);
      if (!select) return;
      const current = mappingFor(field);
      select.innerHTML = '<option value="">— none —</option>' +
        columns.map((col) =>
          '<option value="' + esc(col) + '"' + (col === current ? " selected" : "") + ">" +
          esc(col) + "</option>").join("");
    });

    const customHost = $("sms-custom-fields");
    const mapped = new Set(Object.values(importState.mapping).filter(Boolean));
    const extras = columns.filter((col) => !mapped.has(col));
    customHost.innerHTML = extras.length
      ? extras.map((col) =>
          '<label class="checkbox"><input type="checkbox" class="sms-custom-col" value="' +
          esc(col) + '"> ' + esc(col) + "</label>").join("")
      : '<span class="muted small">All columns are mapped.</span>';

    rebuildStaged();
  }

  function rebuildStaged() {
    if (!importState) return;
    const mapping = importState.mapping;
    const customs = Array.from(document.querySelectorAll(".sms-custom-col:checked"))
      .map((box) => box.value);

    importState.staged = importState.rows.map((row) => {
      const entry = { phone: "", first_name: "", last_name: "", country: "", metadata: {} };
      IMPORT_FIELDS.forEach((field) => {
        const col = mapping[field];
        if (col) entry[field] = String(row[col] == null ? "" : row[col]).trim();
      });
      customs.forEach((col) => { entry.metadata[col] = String(row[col] == null ? "" : row[col]).trim(); });
      return entry;
    });

    const host = $("sms-staging-table");
    if (!importState.staged.length) {
      host.innerHTML = '<p class="muted">No rows to stage.</p>';
      return;
    }
    const headers = IMPORT_FIELDS.concat(customs);
    const head = headers.map((field) => "<th>" + esc(field) + "</th>").join("");
    const body = importState.staged.slice(0, 1000).map((entry, index) => {
      const cells = IMPORT_FIELDS.map((field) =>
        '<td><input class="stage-cell sms-stage-cell" data-row="' + index + '" data-field="' + esc(field) +
        '" data-custom="0" value="' + esc(entry[field]) + '"></td>').join("");
      const customCells = customs.map((col) =>
        '<td><input class="stage-cell sms-stage-cell" data-row="' + index + '" data-field="' + esc(col) +
        '" data-custom="1" value="' + esc(entry.metadata[col] || "") + '"></td>').join("");
      return "<tr>" + cells + customCells + "</tr>";
    }).join("");

    host.innerHTML = '<div class="stage-scroll"><table class="table"><thead><tr>' + head +
      "</tr></thead><tbody>" + body + "</tbody></table></div>" +
      (importState.staged.length > 1000
        ? '<p class="muted small">Showing the first 1000 staged rows of ' + importState.staged.length + ".</p>"
        : "");
  }

  function collectStaged() {
    if (!importState) return [];
    const rows = importState.staged.map((entry) => Object.assign({}, entry, { metadata: Object.assign({}, entry.metadata) }));
    document.querySelectorAll(".sms-stage-cell").forEach((input) => {
      const entry = rows[Number(input.dataset.row)];
      if (!entry) return;
      if (input.dataset.custom === "1") entry.metadata[input.dataset.field] = input.value.trim();
      else entry[input.dataset.field] = input.value.trim();
    });
    return rows.map((entry) => {
      const row = { phone: entry.phone };
      ["first_name", "last_name", "country"].forEach((field) => {
        if (entry[field]) row[field] = entry[field];
      });
      const metadata = {};
      Object.keys(entry.metadata || {}).forEach((key) => {
        if (entry.metadata[key]) metadata[key] = entry.metadata[key];
      });
      if (Object.keys(metadata).length) row.metadata = metadata;
      return row;
    }).filter((row) => row.phone);
  }

  function initSmsImport() {
    const toggle = $("sms-toggle-import");
    const panel = $("sms-import-panel");
    if (toggle && panel) toggle.addEventListener("click", () => { panel.hidden = !panel.hidden; });

    const previewBtn = $("sms-preview-btn");
    if (previewBtn) {
      previewBtn.addEventListener("click", async () => {
        const file = $("sms-import-file").files[0];
        if (!file) { status("sms-import-status", "Choose a file first.", true); return; }
        previewBtn.disabled = true;
        status("sms-import-status", "Reading…", false);
        try {
          const form = new FormData();
          form.append("file", file);
          const data = await upload("/api/sms/contacts/import/preview", form);
          importState = {
            columns: data.columns,
            rows: data.rows,
            mapping: Object.assign({}, data.suggested_mapping),
            staged: [],
          };
          $("sms-mapping-section").hidden = false;
          renderMapping();
          status("sms-import-status",
            data.total_rows + " row(s) found" + (data.truncated ? " (first 5000 staged)" : "") + ".", false);
        } catch (err) {
          status("sms-import-status", "Error: " + err.message, true);
        } finally {
          previewBtn.disabled = false;
        }
      });
    }

    IMPORT_FIELDS.forEach((field) => {
      const select = $("sms-map-" + field);
      if (!select) return;
      select.addEventListener("change", () => {
        importState.mapping[field] = select.value;
        renderMapping();
      });
    });

    const customHost = $("sms-custom-fields");
    if (customHost) customHost.addEventListener("change", rebuildStaged);

    const importBtn = $("sms-import-btn");
    if (importBtn) {
      importBtn.addEventListener("click", async () => {
        if (!importState) return;
        const rows = collectStaged();
        if (!rows.length) { status("sms-import-result", "No rows with a phone number.", true); return; }
        const target = (document.querySelector('input[name="sms-import-target"]:checked') || {}).value || "db";
        const payload = { rows };
        if (target === "existing") {
          const listId = $("sms-import-target-list").value;
          if (listId) payload.list_id = listId;
        } else if (target === "new") {
          const name = $("sms-import-target-name").value.trim();
          if (!name) { status("sms-import-result", "Enter a name for the new list.", true); return; }
          payload.list_name = name;
        }
        importBtn.disabled = true;
        status("sms-import-result", "Saving…", false);
        try {
          const result = await api("/api/sms/contacts/import/commit", payload);
          const invalid = (result.invalid || []).length;
          status("sms-import-result",
            "Imported " + result.imported + " contact(s)" + (result.added_to_list ? ", " + result.added_to_list + " list member(s)" : "") +
            (invalid ? ", skipped " + invalid + " invalid number(s)" : "") + ".", invalid > 0);
          window.setTimeout(() => window.location.reload(), 900);
        } catch (err) {
          status("sms-import-result", "Error: " + err.message, true);
          importBtn.disabled = false;
        }
      });
    }

    const addBtn = $("sms-add-contact-btn");
    if (addBtn) {
      addBtn.addEventListener("click", async () => {
        const payload = {
          phone: $("sms-new-phone").value.trim(),
          first_name: $("sms-new-first").value.trim() || undefined,
          last_name: $("sms-new-last").value.trim() || undefined,
        };
        if (!payload.phone) { status("sms-add-status", "Enter a phone number.", true); return; }
        addBtn.disabled = true;
        try {
          await api("/api/sms/contacts", payload);
          status("sms-add-status", "Contact created.", false);
          $("sms-new-phone").value = "";
          $("sms-new-first").value = "";
          $("sms-new-last").value = "";
          window.setTimeout(() => window.location.reload(), 700);
        } catch (err) {
          status("sms-add-status", "Error: " + err.message, true);
          addBtn.disabled = false;
        }
      });
    }
  }

  function selectedContactIds() {
    return Array.from(document.querySelectorAll(".sms-contact-select:checked")).map((box) => box.value);
  }

  function initContactsTable() {
    const table = $("sms-contacts-table");
    if (!table) return;

    const selectAll = $("sms-select-all");
    const countEl = $("sms-selected-count");
    const refreshCount = () => {
      if (countEl) countEl.textContent = selectedContactIds().length + " selected";
    };
    table.addEventListener("change", (event) => {
      if (event.target.classList.contains("sms-contact-select")) refreshCount();
    });
    if (selectAll) {
      selectAll.addEventListener("change", () => {
        document.querySelectorAll(".sms-contact-select").forEach((box) => { box.checked = selectAll.checked; });
        refreshCount();
      });
    }

    table.addEventListener("click", async (event) => {
      const save = event.target.closest("[data-sms-save-contact]");
      if (save) {
        const row = save.closest("tr");
        const payload = {};
        row.querySelectorAll(".inline-edit").forEach((input) => {
          const value = input.value.trim();
          if (value) payload[input.dataset.field] = value;
        });
        save.disabled = true;
        try {
          await api("/api/sms/contacts/" + save.dataset.smsSaveContact, payload, "PATCH");
          save.textContent = "Saved";
          window.setTimeout(() => window.location.reload(), 500);
        } catch (err) {
          window.alert("Error: " + err.message);
          save.disabled = false;
        }
        return;
      }

      const hide = event.target.closest("[data-sms-hide-contact]");
      if (hide) {
        await api("/api/sms/contacts/soft-delete", { ids: [hide.dataset.smsHideContact] });
        window.location.reload();
        return;
      }

      const restore = event.target.closest("[data-sms-restore-contact]");
      if (restore) {
        await api("/api/sms/contacts/restore", { ids: [restore.dataset.smsRestoreContact] });
        window.location.reload();
        return;
      }

      const del = event.target.closest("[data-sms-delete-contact]");
      if (del) {
        if (!window.confirm("Permanently delete this contact? Campaign history is removed too.")) return;
        await api("/api/sms/contacts/" + del.dataset.smsDeleteContact, {}, "DELETE");
        window.location.reload();
      }
    });

    const bind = (id, handler) => {
      const button = $(id);
      if (button) button.addEventListener("click", handler);
    };

    bind("sms-hide-selected-btn", async () => {
      const ids = selectedContactIds();
      if (!ids.length) return;
      await api("/api/sms/contacts/soft-delete", { ids });
      window.location.reload();
    });
    bind("sms-restore-selected-btn", async () => {
      const ids = selectedContactIds();
      if (!ids.length) return;
      await api("/api/sms/contacts/restore", { ids });
      window.location.reload();
    });
    bind("sms-delete-selected-btn", async () => {
      const ids = selectedContactIds();
      if (!ids.length) return;
      if (!window.confirm("Permanently delete " + ids.length + " contact(s)?")) return;
      await api("/api/sms/contacts/bulk-delete", { ids });
      window.location.reload();
    });
    bind("sms-add-selected-btn", async () => {
      const ids = selectedContactIds();
      const listId = $("sms-selected-list-target").value;
      if (!ids.length) { window.alert("Select contacts first."); return; }
      if (!listId) { window.alert("Choose a list first."); return; }
      const result = await api("/api/sms/lists/" + listId + "/members", { contact_ids: ids });
      window.alert("Added " + result.added + " member(s).");
      window.location.reload();
    });
  }

  function initLists() {
    const form = $("sms-list-create-form");
    if (form) {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("button[type=submit]");
        button.disabled = true;
        try {
          const result = await api("/api/sms/lists", {
            name: form.elements.name.value.trim(),
            description: formValue(form, "description"),
            phones: splitList(form.elements.phones.value),
          });
          const invalid = (result.invalid || []).length;
          status("sms-list-status", "List created" + (invalid ? " (skipped " + invalid + " invalid number(s))" : "") + ".", invalid > 0);
          window.setTimeout(() => window.location.reload(), 700);
        } catch (err) {
          status("sms-list-status", "Error: " + err.message, true);
          button.disabled = false;
        }
      });
    }

    document.querySelectorAll("[data-sms-list-toggle]").forEach((button) => {
      button.addEventListener("click", async () => {
        const listId = button.dataset.smsListToggle;
        const host = $("sms-members-" + listId);
        if (!host) return;
        host.hidden = !host.hidden;
        if (host.hidden) return;
        host.innerHTML = '<p class="muted">Loading…</p>';
        try {
          const data = await api("/api/sms/lists/" + listId);
          const members = data.members || [];
          const rows = members.map((member) =>
            "<tr><td>" + esc(member.phone) + "</td><td>" + esc(member.first_name || "") + "</td><td>" +
            esc(member.last_name || "") + '</td><td class="num"><button type="button" class="btn btn-sm btn-danger" data-sms-member-remove="' +
            esc(listId) + '" data-sms-member-id="' + esc(member.contact_id || member.id) + '">Remove</button></td></tr>'
          ).join("");
          host.innerHTML = (members.length
            ? '<table class="table"><thead><tr><th>Phone</th><th>First name</th><th>Last name</th><th></th></tr></thead><tbody>' + rows + "</tbody></table>"
            : '<p class="muted">This list has no members yet.</p>') +
            '<div class="inline-row add-member-form"><input type="tel" class="sms-member-phone" placeholder="+15551234567">' +
            '<button type="button" class="btn btn-sm" data-sms-member-add="' + esc(listId) + '">Add phone</button>' +
            '<span class="muted small sms-member-status"></span></div>';
        } catch (err) {
          host.innerHTML = '<p class="status error">' + esc(err.message) + "</p>";
        }
      });
    });

    document.addEventListener("click", async (event) => {
      const add = event.target.closest("[data-sms-member-add]");
      if (add) {
        const host = add.closest(".list-members");
        const input = host.querySelector(".sms-member-phone");
        const label = host.querySelector(".sms-member-status");
        try {
          const result = await api("/api/sms/lists/" + add.dataset.smsMemberAdd + "/members",
            { phones: splitList(input.value) });
          label.textContent = "Added " + result.added + ".";
          window.setTimeout(() => window.location.reload(), 600);
        } catch (err) {
          label.textContent = "Error: " + err.message;
          label.style.color = "var(--danger)";
        }
        return;
      }

      const remove = event.target.closest("[data-sms-member-remove]");
      if (remove) {
        await api("/api/sms/lists/" + remove.dataset.smsMemberRemove + "/members/" +
          remove.dataset.smsMemberId, {}, "DELETE");
        remove.closest("tr").remove();
        return;
      }

      const del = event.target.closest("[data-sms-delete-list]");
      if (del) {
        if (!window.confirm("Delete this list? Contacts stay in the address book.")) return;
        await api("/api/sms/lists/" + del.dataset.smsDeleteList, {}, "DELETE");
        window.location.reload();
      }
    });
  }

  // ------------------------------------------------------------------ //
  // Templates
  // ------------------------------------------------------------------ //
  let templateData = [];

  function readTemplatesData() {
    const host = $("sms-templates-data");
    if (!host) return [];
    try { return JSON.parse(host.textContent) || []; } catch (_) { return []; }
  }

  async function previewMessage(content, includeStop, unicode) {
    return api("/api/sms/templates/preview", {
      content,
      include_stop_code: Boolean(includeStop),
      unicode_enabled: typeof unicode === "boolean" ? unicode : undefined,
    });
  }

  function renderPreview(prefix, data) {
    const body = $(prefix + "-body");
    if (body) body.textContent = data.content || "";
    const meta = $(prefix + "-meta");
    if (meta) {
      meta.textContent = data.segments + " segment(s) · " + data.encoding +
        " · " + (data.segments || 1) + " credit(s)";
    }
  }

  function initTemplates() {
    const form = $("sms-template-form");
    if (!form) return;
    templateData = readTemplatesData();

    const content = form.elements.content;
    const title = $("sms-template-editor-title");
    let previewTimer = null;

    const schedulePreview = () => {
      window.clearTimeout(previewTimer);
      previewTimer = window.setTimeout(async () => {
        if (!content.value.trim()) return;
        try {
          const data = await previewMessage(content.value, false, form.elements.unicode_enabled.checked);
          $("sms-template-preview").hidden = false;
          renderPreview("sms-template-preview", data);
        } catch (_) { /* live preview is best-effort */ }
      }, 450);
    };
    content.addEventListener("input", schedulePreview);

    const reset = () => {
      form.reset();
      form.elements.id.value = "";
      if (title) title.textContent = "New template";
      $("sms-template-preview").hidden = true;
      status("sms-template-status", "", false);
    };

    const load = (id) => {
      const template = templateData.find((entry) => entry.id === id);
      if (!template) return;
      form.elements.id.value = template.id;
      form.elements.name.value = template.name || "";
      form.elements.content.value = template.content || "";
      form.elements.sms_type.value = template.sms_type || "transactional";
      form.elements.unicode_enabled.checked = Boolean(template.unicode_enabled);
      form.elements.organisation_prefix.value = template.organisation_prefix || "";
      if (title) title.textContent = "Edit template";
      schedulePreview();
      form.scrollIntoView({ behavior: "smooth" });
    };

    document.querySelectorAll("[data-sms-template-edit]").forEach((button) => {
      button.addEventListener("click", () => load(button.dataset.smsTemplateEdit));
    });

    document.querySelectorAll("[data-sms-template-use]").forEach((button) => {
      button.addEventListener("click", () => {
        window.location.href = "/sms/campaigns/new?template=" + encodeURIComponent(button.dataset.smsTemplateUse);
      });
    });

    document.querySelectorAll("[data-sms-template-delete]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!window.confirm("Delete this template?")) return;
        try {
          await api("/api/sms/templates/" + button.dataset.smsTemplateDelete, {}, "DELETE");
          window.location.reload();
        } catch (err) {
          window.alert("Error: " + err.message);
        }
      });
    });

    document.querySelectorAll("[data-sms-merge]").forEach((button) => {
      button.addEventListener("click", () => {
        const tag = button.dataset.smsMerge;
        const start = content.selectionStart || content.value.length;
        content.value = content.value.slice(0, start) + tag + content.value.slice(content.selectionEnd || start);
        content.focus();
        content.selectionStart = content.selectionEnd = start + tag.length;
        schedulePreview();
      });
    });

    const newBtn = $("sms-template-new-btn");
    if (newBtn) newBtn.addEventListener("click", () => { reset(); form.scrollIntoView({ behavior: "smooth" }); });
    const resetBtn = $("sms-template-reset-btn");
    if (resetBtn) resetBtn.addEventListener("click", reset);

    const previewBtn = $("sms-template-preview-btn");
    if (previewBtn) {
      previewBtn.addEventListener("click", async () => {
        if (!content.value.trim()) { status("sms-template-status", "Write a message first.", true); return; }
        try {
          const data = await previewMessage(content.value, false, form.elements.unicode_enabled.checked);
          $("sms-template-preview").hidden = false;
          renderPreview("sms-template-preview", data);
        } catch (err) {
          status("sms-template-status", "Error: " + err.message, true);
        }
      });
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("sms-template-save-btn");
      button.disabled = true;
      const id = form.elements.id.value;
      const payload = {
        name: form.elements.name.value.trim(),
        content: form.elements.content.value,
        sms_type: form.elements.sms_type.value,
        unicode_enabled: form.elements.unicode_enabled.checked,
      };
      const prefix = form.elements.organisation_prefix.value.trim();
      if (prefix) payload.organisation_prefix = prefix;
      try {
        await api(id ? "/api/sms/templates/" + id : "/api/sms/templates", payload, id ? "PATCH" : "POST");
        status("sms-template-status", "Saved.", false);
        window.setTimeout(() => window.location.reload(), 600);
      } catch (err) {
        status("sms-template-status", "Error: " + err.message, true);
        button.disabled = false;
      }
    });
  }

  // ------------------------------------------------------------------ //
  // Campaign composer
  // ------------------------------------------------------------------ //
  function initCampaignForm() {
    const form = $("sms-campaign-form");
    if (!form) return;

    const content = form.elements.content;
    const campaignId = form.dataset.campaignId || "";
    let startAfterSave = false;
    let previewTimer = null;

    const preview = () => {
      window.clearTimeout(previewTimer);
      previewTimer = window.setTimeout(async () => {
        if (!content.value.trim()) return;
        try {
          const data = await previewMessage(
            content.value,
            form.elements.include_stop_code.checked,
            form.elements.unicode_enabled.checked
          );
          renderPreview("sms-preview", data);
        } catch (_) { /* best effort */ }
      }, 400);
    };
    content.addEventListener("input", preview);
    form.elements.include_stop_code.addEventListener("change", preview);
    form.elements.unicode_enabled.addEventListener("change", preview);
    form.elements.sms_type.addEventListener("change", () => {
      if (form.elements.sms_type.value === "marketing") form.elements.include_stop_code.checked = true;
      preview();
    });

    document.querySelectorAll("[data-sms-merge]").forEach((button) => {
      button.addEventListener("click", () => {
        const tag = button.dataset.smsMerge;
        const start = content.selectionStart || content.value.length;
        content.value = content.value.slice(0, start) + tag + content.value.slice(content.selectionEnd || start);
        content.focus();
        content.selectionStart = content.selectionEnd = start + tag.length;
        preview();
      });
    });

    const templates = readTemplatesData();
    const templateSelect = $("sms-template-select");
    if (templateSelect) {
      templateSelect.addEventListener("change", () => {
        const template = templates.find((entry) => entry.id === templateSelect.value);
        if (!template) return;
        content.value = template.content || "";
        if (template.sms_type) form.elements.sms_type.value = template.sms_type;
        form.elements.unicode_enabled.checked = Boolean(template.unicode_enabled);
        if (template.sms_type === "marketing") form.elements.include_stop_code.checked = true;
        if (!form.elements.name.value.trim()) form.elements.name.value = template.name;
        preview();
      });

      const requested = new URLSearchParams(window.location.search).get("template");
      if (requested) {
        templateSelect.value = requested;
        templateSelect.dispatchEvent(new Event("change"));
      }
    }

    if (!campaignId && !templateSelect) preview();

    const startBtn = $("sms-save-start-btn");
    if (startBtn) {
      startBtn.addEventListener("click", () => { startAfterSave = true; });
    }

    if (content.value.trim()) preview();

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const buttons = form.querySelectorAll("button[type=submit]");
      buttons.forEach((b) => { b.disabled = true; });
      status("sms-campaign-status", "Saving…", false);

      const listIds = Array.from(document.querySelectorAll(".sms-list-picker-item:checked"))
        .map((box) => box.value);
      const payload = {
        name: form.elements.name.value.trim(),
        content: form.elements.content.value,
        sender: formValue(form, "sender"),
        sms_type: form.elements.sms_type.value,
        unicode_enabled: form.elements.unicode_enabled.checked,
        include_stop_code: form.elements.include_stop_code.checked,
        priority: form.elements.priority.value,
        batch_size: numberValue(form, "batch_size"),
        send_rate_per_minute: numberValue(form, "send_rate_per_minute"),
        stagger_seconds: numberValue(form, "stagger_seconds"),
        window_timezone: formValue(form, "window_timezone"),
        soft_bounce_policy: form.elements.soft_bounce_policy.value,
        soft_bounce_threshold: numberValue(form, "soft_bounce_threshold"),
        organisation_prefix: formValue(form, "organisation_prefix"),
        schedule_at: formValue(form, "schedule_at"),
        window_start: formValue(form, "window_start"),
        window_end: formValue(form, "window_end"),
      };
      Object.keys(payload).forEach((key) => {
        if (payload[key] === undefined) delete payload[key];
      });

      try {
        let saved;
        if (campaignId) {
          saved = await api("/api/sms/campaigns/" + campaignId, payload, "PATCH");
          await api("/api/sms/campaigns/" + campaignId + "/lists",
            { list_ids: listIds, attach: true });
          window.location.href = "/sms/campaigns/" + campaignId;
          return;
        }

        payload.list_ids = listIds;
        const phones = splitList(form.elements.recipient_phones ? form.elements.recipient_phones.value : "");
        if (phones.length) payload.recipient_phones = phones;
        saved = await api("/api/sms/campaigns", payload);
        const newId = saved.campaign.id;
        if (startAfterSave) {
          try {
            await api("/api/sms/campaigns/" + newId + "/action", { action: "start" });
          } catch (err) {
            window.alert("Campaign saved, but it could not start: " + err.message);
          }
        }
        window.location.href = "/sms/campaigns/" + newId;
      } catch (err) {
        status("sms-campaign-status", "Error: " + err.message, true);
        buttons.forEach((b) => { b.disabled = false; });
      }
    });
  }

  // ------------------------------------------------------------------ //
  // Campaign detail: actions, test, reuse
  // ------------------------------------------------------------------ //
  function initCampaignDetail() {
    // Actions need the campaign id from the URL path.
    const parts = window.location.pathname.split("/").filter(Boolean);
    const campaignId = parts[parts.length - 1];

    document.querySelectorAll("[data-sms-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const action = button.dataset.smsAction;
        if (action === "cancel" && !window.confirm("Cancel this campaign? Remaining recipients will be skipped.")) return;
        button.disabled = true;
        status("sms-action-status", "Working…", false);
        try {
          await api("/api/sms/campaigns/" + campaignId + "/action", { action });
          window.location.reload();
        } catch (err) {
          status("sms-action-status", "Error: " + err.message, true);
          button.disabled = false;
        }
      });
    });

    const deleteBtn = document.querySelector("[data-sms-delete-campaign]");
    if (deleteBtn) {
      deleteBtn.addEventListener("click", async () => {
        if (!window.confirm("Permanently delete this campaign?")) return;
        try {
          await api("/api/sms/campaigns/" + deleteBtn.dataset.smsDeleteCampaign, {}, "DELETE");
          window.location.href = "/sms";
        } catch (err) {
          window.alert("Error: " + err.message);
        }
      });
    }

    const testForm = $("sms-test-form");
    if (testForm) {
      testForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = testForm.querySelector("button[type=submit]");
        button.disabled = true;
        status("sms-test-status", "Sending…", false);
        try {
          const result = await api("/api/sms/campaigns/" + testForm.dataset.campaignId + "/send-test",
            { to: $("sms-test-to").value.trim() });
          status("sms-test-status",
            "Sent to " + result.to + " (" + result.segments + " segment(s), message " + result.message_id + ").", false);
        } catch (err) {
          status("sms-test-status", "Error: " + err.message, true);
        } finally {
          button.disabled = false;
        }
      });
    }

    const modeSelect = $("sms-reuse-mode");
    document.querySelectorAll("[data-sms-reuse]").forEach((button) => {
      button.addEventListener("click", async () => {
        const kind = button.dataset.smsReuse;
        const mode = modeSelect ? modeSelect.value : "new";
        if (kind === "resend" && !window.confirm("Resend this campaign to the selected recipients now?")) return;
        button.disabled = true;
        status("sms-reuse-status", "Working…", false);
        try {
          const result = await api("/api/sms/campaigns/" + campaignId + "/" + kind, { mode });
          if (kind === "clone") {
            window.location.href = "/sms/campaigns/" + result.campaign.id + "/edit";
          } else {
            window.location.reload();
          }
        } catch (err) {
          status("sms-reuse-status", "Error: " + err.message, true);
          button.disabled = false;
        }
      });
    });
  }

  // ------------------------------------------------------------------ //
  // Settings
  // ------------------------------------------------------------------ //
  function initSettings() {
    const form = $("sms-settings-form");
    if (!form) return;

    let plans = {};
    const plansHost = $("sms-plans-data");
    if (plansHost) {
      try { plans = JSON.parse(plansHost.textContent) || {}; } catch (_) { plans = {}; }
    }

    const renderPlanHint = () => {
      const hint = $("sms-plan-hint");
      const spec = plans[form.elements.sms_plan.value];
      if (!hint || !spec) return;
      hint.innerHTML = "<strong>" + esc(spec.label) + "</strong> &middot; " + esc(spec.note) +
        "<br>Recommended: " + spec.recommended.send_rate_per_minute + "/min, " +
        spec.recommended.max_recipients_per_hour + "/hour, " +
        spec.recommended.max_recipients_per_day + "/day, " +
        spec.recommended.max_credits_per_day + " credits/day.<br>" +
        "Hard limits: " + spec.limits.send_rate_per_minute + "/min, " +
        spec.limits.max_recipients_per_hour + "/hour, " +
        spec.limits.max_recipients_per_day + "/day, " +
        spec.limits.max_credits_per_day + " credits/day.";
    };
    form.elements.sms_plan.addEventListener("change", renderPlanHint);
    renderPlanHint();

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector("button[type=submit]");
      button.disabled = true;
      status("sms-settings-status", "Saving…", false);

      const payload = {
        sms_plan: form.elements.sms_plan.value,
        sms_sender: formValue(form, "sms_sender"),
        sms_sender_type: form.elements.sms_sender_type.value,
        default_sms_type: form.elements.default_sms_type.value,
        unicode_enabled: form.elements.unicode_enabled.checked,
        include_stop_code: form.elements.include_stop_code.checked,
        sending_paused: form.elements.sending_paused.checked,
        default_country_code: formValue(form, "default_country_code"),
        organisation_prefix: formValue(form, "organisation_prefix"),
        public_base_url: formValue(form, "public_base_url"),
        low_credit_threshold: numberValue(form, "low_credit_threshold"),
        credit_cost_per_segment: numberValue(form, "credit_cost_per_segment"),
        max_hard_bounce_rate: numberValue(form, "max_hard_bounce_rate"),
        max_complaint_rate: numberValue(form, "max_complaint_rate"),
        send_rate_per_minute: numberValue(form, "send_rate_per_minute"),
        default_batch_size: numberValue(form, "default_batch_size"),
        max_recipients_per_hour: numberValue(form, "max_recipients_per_hour"),
        max_recipients_per_day: numberValue(form, "max_recipients_per_day"),
        max_credits_per_day: numberValue(form, "max_credits_per_day"),
        default_stagger_seconds: numberValue(form, "default_stagger_seconds"),
        collision_window_seconds: numberValue(form, "collision_window_seconds"),
        cross_gap_min_seconds: numberValue(form, "cross_gap_min_seconds"),
        cross_gap_max_seconds: numberValue(form, "cross_gap_max_seconds"),
        default_window_timezone: formValue(form, "default_window_timezone"),
        default_window_start: formValue(form, "default_window_start"),
        default_window_end: formValue(form, "default_window_end"),
        warmup_enabled: form.elements.warmup_enabled.checked,
        warmup_start_date: formValue(form, "warmup_start_date"),
        telegram_bot_token: formValue(form, "telegram_bot_token"),
        telegram_chat_id: formValue(form, "telegram_chat_id"),
      };
      Object.keys(payload).forEach((key) => {
        if (payload[key] === undefined || payload[key] === "") delete payload[key];
      });

      try {
        const result = await api("/api/sms/settings", payload, "PATCH");
        const warnings = result.warnings || [];
        status("sms-settings-status",
          warnings.length ? "Saved with adjustments: " + warnings.join(" ") : "Saved.", warnings.length > 0);
        loadCredits(false);
      } catch (err) {
        status("sms-settings-status", "Error: " + err.message, true);
      } finally {
        button.disabled = false;
      }
    });

    const testTgBtn = document.getElementById("test-sms-telegram-btn");
    if (testTgBtn) {
      testTgBtn.addEventListener("click", async () => {
        testTgBtn.disabled = true;
        status("sms-settings-status", "Sending Telegram test...", false);
        try {
          await api("/api/settings/test-telegram", { section: "b_sms" }, "POST");
          status("sms-settings-status", "Telegram test sent - check your chat.", false);
        } catch (err) {
          status("sms-settings-status", "Telegram test failed: " + err.message, true);
        } finally {
          testTgBtn.disabled = false;
        }
      });
    }

    const clearTgBtn = document.getElementById("clear-sms-telegram-btn");
    if (clearTgBtn) {
      clearTgBtn.addEventListener("click", async () => {
        clearTgBtn.disabled = true;
        status("sms-settings-status", "Clearing Telegram overrides...", false);
        try {
          await api("/api/sms/settings", { telegram_bot_token: "", telegram_chat_id: "" }, "PATCH");
          form.elements.telegram_bot_token.value = "";
          form.elements.telegram_chat_id.value = "";
          status("sms-settings-status", "Telegram overrides cleared; using global fallback.", false);
        } catch (err) {
          status("sms-settings-status", "Clear failed: " + err.message, true);
        } finally {
          clearTgBtn.disabled = false;
        }
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    [initCredits, initSeeMore, initSmsImport, initContactsTable, initLists,
     initTemplates, initCampaignForm, initCampaignDetail, initSettings]
      .forEach((init) => {
        try {
          init();
        } catch (err) {
          console.error("sms init failed:", init.name, err);
        }
      });
    loadCredits(false);
  });
})();
