// Dashboard behaviour: campaign create/edit (incl. rich-text toggle), settings,
// contact import with column mapping, and campaign actions. Mutations call the
// existing JSON API (single source of truth).

function csrfToken() {
  const token = document.querySelector('meta[name="csrf-token"]');
  return token && token.content ? token.content : "";
}

async function postJSON(url, body, method = "POST") {
  const headers = { "Content-Type": "application/json" };
  if (csrfToken()) headers["X-CSRFToken"] = csrfToken();
  const res = await fetch(url, {
    method,
    headers,
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) {
    const parts = [];
    if (data.error) parts.push(typeof data.error === "string" ? data.error : JSON.stringify(data.error));
    if (Array.isArray(data.reasons)) parts.push(data.reasons.join(" "));
    throw new Error(parts.join(" ") || res.statusText);
  }
  return data;
}

function setStatus(id, text, isError) {
  const el = typeof id === "string" ? document.getElementById(id) : id;
  if (el) {
    el.textContent = text;
    el.style.color = isError ? "var(--danger)" : "var(--muted)";
  }
}

function stripHtml(html) {
  const tmp = document.createElement("div");
  tmp.innerHTML = html || "";
  return (tmp.textContent || "").trim();
}

function escapeHtml(value) {
  const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(value ?? "").replace(/[&<>"']/g, (c) => map[c]);
}

// ------------------------------------------------------------------ //
// Info pop-ups: a single registry of setting explanations. Templates add
// `<button type="button" class="info-icon" data-info="KEY">i</button>`.
// ------------------------------------------------------------------ //
const INFO = {
  sender_email: {
    title: "Default from email",
    body: "<p>The address recipients see. It <strong>must</strong> use your authenticated " +
      "sending domain, otherwise Gmail/Outlook will treat it as spoofing.</p>" +
      "<p><em>Example:</em> <code>news@yourdomain.com</code></p>",
  },
  sender_name: {
    title: "Default from name",
    body: "<p>The friendly name shown next to the address. Use a recognizable brand name.</p>" +
      "<p>Avoid <code>no-reply</code> addresses in the email — they suppress replies and " +
      "look more like bulk mail. <em>Example:</em> <code>Acme News</code></p>",
  },
  sender_domain: {
    title: "Sending domain",
    body: "<p>The domain you verified in Brevo (DKIM + DMARC). Every campaign From address " +
      "must be on this domain (or one added to the allow-list).</p>" +
      "<p><em>Example:</em> <code>yourdomain.com</code></p>",
  },
  allowed_sender_domains: {
    title: "Additional allowed domains",
    body: "<p>Extra domains you have also verified in Brevo. Enter one per line. " +
      "Campaigns may send From any domain listed here or the primary sending domain.</p>",
  },
  reply_to_email: {
    title: "Reply-to address",
    body: "<p>Where replies go. A real, monitored mailbox improves trust and deliverability " +
      "versus a <code>no-reply</code> address.</p><p><em>Example:</em> <code>hello@yourdomain.com</code></p>",
  },
  operator_email: {
    title: "Operator email",
    body: "<p>Where test emails and operational notices are sent. If blank, the sender " +
      "address is used.</p>",
  },
  sms_sender: {
    title: "B SMS sender ID",
    body: "<p>Brevo accepts up to 11 alphanumeric characters or 15 digits. " +
      "Recipients see this instead of a phone number; in some countries it must be pre-registered.</p>" +
      "<p><em>Example:</em> <code>Acme</code></p>",
  },
  sms_plan: {
    title: "Brevo SMS plan",
    body: "<p>Your Brevo subscription tier. It sets recommended pacing, hourly/daily recipient caps " +
      "and credit ceilings enforced before every send.</p>",
  },
  sms_unicode: {
    title: "Unicode SMS",
    body: "<p>GSM-7 fits 160 characters per segment; Unicode/UCS-2 fits only 70. " +
      "Enable Unicode only for emoji or non-Latin scripts because it can triple the credit cost.</p>",
  },
  sms_stop: {
    title: "STOP line",
    body: "<p>Appends opt-out instructions to marketing SMS. Transactional messages normally omit it. " +
      "The line itself consumes characters and can add a segment.</p>",
  },
  sms_schedule: {
    title: "SMS scheduling",
    body: "<p>Drafts send immediately when started. Set a schedule to hold delivery until a specific time. " +
      "Quiet windows and timezone checks are applied at send time.</p>",
  },
  bird_sender: {
    title: "Bird sender",
    body: "<p>The phone number or sender ID recipients see. Use an E.164 number or an " +
      "alphanumeric sender ID enabled in Bird. The sender must be provisioned for every " +
      "destination country or Bird will reject the message.</p>" +
      "<p><em>Examples:</em> <code>+15551234567</code> or <code>Acme</code></p>",
  },
  bird_sender_type: {
    title: "Bird sender type",
    body: "<p>Tells Bird whether the sender is a long/short phone number or an alphanumeric " +
      "sender ID. Choose the same type that is registered in your Bird workspace. A mismatch " +
      "can make Bird reject otherwise valid messages.</p>",
  },
  bird_category: {
    title: "Bird message category",
    body: "<p>Bird routes by category: <code>transactional</code>, <code>marketing</code>, " +
      "<code>authentication</code> or <code>service</code>. The category affects consent rules, " +
      "STOP handling and routing.</p><p>Use <strong>marketing</strong> for promotional messages, " +
      "<strong>transactional</strong> for account/order updates, and <strong>authentication</strong> " +
      "for one-time codes.</p>",
  },
  bird_smart_encoding: {
    title: "Bird smart encoding",
    body: "<p>Lets Bird choose GSM-7 when the text allows it and fall back to UCS-2 for emoji or " +
      "non-Latin characters. GSM-7 fits 160 characters per segment; UCS-2 fits only 70, so " +
      "emoji can sharply increase segment count and cost.</p>",
  },
  bird_budget: {
    title: "Bird daily budget",
    body: "<p>The worker stops starting new sends when today&rsquo;s estimated cost reaches this " +
      "amount. Bird does not expose a wallet balance to ordinary SMS workspace keys, so this is a " +
      "local safety limit, not a real account balance.</p>" +
      "<p><em>Example:</em> <code>20</code> means at most about $20 of estimated spend per day.</p>",
  },
  bird_key: {
    title: "Bird API key",
    body: "<p>A workspace key beginning with <code>bk_</code>. It needs SMS send permission. " +
      "Add the <code>sms_management</code> scope if you want Bird template catalogue reads. " +
      "The key is stored server-side and never returned in full by the API.</p>",
  },
  bird_management_key: {
    title: "Bird management API key",
    body: "<p>Optional. Use this only when the main Bird API key cannot read the template " +
      "catalogue. It needs the <code>sms_management</code> scope.</p>" +
      "<p>If left blank, the app reuses <code>BIRD_API_KEY</code>; a key with SMS sending plus " +
      "<code>sms_management</code> read/write works for both jobs.</p>",
  },
  bird_webhook_secret: {
    title: "Bird webhook signing secret",
    body: "<p>Starts with <code>whsec_</code>. Bird signs every webhook with Standard Webhooks " +
      "HMAC-SHA256 over the request ID, timestamp and raw body. Without the correct secret, " +
      "delivery updates and STOP events are rejected.</p>" +
      "<p>Copy it from the Bird webhook configuration, not from the API key screen.</p>",
  },
  bird_region: {
    title: "Bird region",
    body: "<p>Select the region shown in your Bird workspace. Requests go to " +
      "<code>us1.platform.bird.com</code> or <code>eu1.platform.bird.com</code>. The wrong region " +
      "fails authentication even when the API key is valid.</p>",
  },
  bird_workspace: {
    title: "Bird workspace ID",
    body: "<p>Optional reference only. The API key already identifies the workspace, and Bird " +
      "SMS requests do not send this value. Keep it if it helps operators match this app to the " +
      "correct Bird workspace; it does not affect sending.</p>",
  },
  bird_public_base_url: {
    title: "Bird public base URL",
    body: "<p>The public HTTPS address Bird can reach. The app shows a webhook URL built from " +
      "this value: <code>&lt;base&gt;/api/webhooks/bird</code>.</p>" +
      "<p>If blank, the app falls back to the general public base URL. Never use localhost for " +
      "live webhooks because Bird cannot deliver to it.</p>" +
      "<p><em>Example:</em> <code>https://track.yourdomain.com</code></p>",
  },
  bird_include_stop: {
    title: "Append a STOP line",
    body: "<p>Adds opt-out instructions such as <code>Reply STOP to opt out</code>. Marketing SMS " +
      "should normally include it; transactional and authentication messages often omit it.</p>" +
      "<p>The line consumes characters and can add a segment to the message.</p>",
  },
  bird_template: {
    title: "Bird templates",
    body: "<p>Template sends use a slug, language and JSON parameters instead of free text. " +
      "System templates reject a sender; workspace templates require an approved sender. " +
      "The catalogue needs the <code>sms_management</code> scope, but you can always type a slug manually.</p>",
  },
  bird_template_scope: {
    title: "Bird template scope",
    body: "<p><strong>System</strong> templates are shared by Bird and must not include a " +
      "<code>from</code> sender. <strong>Workspace</strong> templates belong to your account and " +
      "require an approved sender. Leave it unset only when you know Bird can infer the scope.</p>",
  },
  bird_template_slug: {
    title: "Bird template slug",
    body: "<p>The template identifier from Bird, not its display name. Use the slug shown in the " +
      "Bird template catalogue.</p><p><em>Example:</em> <code>order_ready</code></p>",
  },
  bird_template_language: {
    title: "Bird template language",
    body: "<p>The template language code registered in Bird. Use a short code such as " +
      "<code>en</code>, <code>de</code> or <code>fr</code>. The slug and language must match a " +
      "template that exists in the selected scope.</p>",
  },
  bird_template_name: {
    title: "Bird template name",
    body: "<p>A human-readable name for this campaign&rsquo;s template. It helps operators " +
      "identify the template later; Bird uses the slug and language to resolve the actual template.</p>",
  },
  bird_template_parameters: {
    title: "Bird template parameters",
    body: "<p>A JSON object containing the values substituted into the Bird template. Keys must " +
      "match the placeholders in the template.</p>" +
      "<p><em>Example:</em> <code>{\"first_name\": \"Ada\", \"order_id\": \"A100\"}</code></p>" +
      "<p>This field must contain valid JSON or the campaign will fail validation.</p>",
  },
  bird_template_catalogue: {
    title: "Bird template catalogue",
    body: "<p>Read-only list of templates available to the workspace, fetched from Bird. It " +
      "requires the <code>sms_management</code> scope. If the catalogue is unavailable, type the " +
      "slug from the Bird dashboard instead; sending still works.</p>",
  },
  bird_template_content: {
    title: "Bird template content",
    body: "<p>The message body saved for reuse in this workspace. Templates are copied into new " +
      "campaigns, so later edits do not change campaigns that already exist.</p>" +
      "<p>Merge tags: <code>{{first_name}}</code>, <code>{{last_name}}</code> and " +
      "<code>{{phone}}</code>. Bird caps a message at 12 segments.</p>",
  },
  bird_template_preview: {
    title: "Template preview and cost",
    body: "<p>Shows the rendered message and estimated segment count before saving. The estimate " +
      "assumes a typical recipient; real costs vary with merge-tag length and encoding.</p>",
  },
  bird_quiet_window: {
    title: "Bird quiet window",
    body: "<p>Sends are held outside this local-time window. Use an IANA timezone such as " +
      "<code>Europe/London</code>; the window is evaluated at dispatch time. Leave both times " +
      "blank to send at any hour.</p>",
  },
  bird_window_timezone: {
    title: "Quiet-window timezone",
    body: "<p>The IANA timezone used to interpret the quiet-window start and end times.</p>" +
      "<p><em>Example:</em> <code>Europe/London</code> or <code>America/New_York</code>. " +
      "Do not use an abbreviation such as <code>EST</code>, because daylight saving would be wrong.</p>",
  },
  bird_schedule: {
    title: "Bird campaign schedule",
    body: "<p>Optional UTC date and time to hold the campaign until it should start. While " +
      "scheduled, the campaign does not send. Quiet windows and daily caps are still checked at " +
      "dispatch time.</p>",
  },
  bird_campaign_name: {
    title: "Bird campaign name",
    body: "<p>An internal label for the campaign. Recipients do not see it. Use a name that " +
      "identifies the audience and purpose, for example <code>October launch &mdash; UK leads</code>.</p>",
  },
  bird_priority: {
    title: "Bird campaign priority",
    body: "<p>High priority lets this campaign take the shared SMS sending budget while it has " +
      "recipients left. Other active campaigns wait without being paused and resume automatically.</p>" +
      "<p>Only one high-priority campaign is allowed at a time.</p>",
  },
  bird_message_mode: {
    title: "Bird message mode",
    body: "<p><strong>Free text</strong> sends the body you type and supports merge tags. " +
      "<strong>Bird template</strong> sends a template by slug and language with JSON parameters. " +
      "Template sends are resolved by Bird and do not use the free-text body.</p>",
  },
  bird_message_body: {
    title: "Bird message body",
    body: "<p>The SMS text sent to recipients. Merge tags: <code>{{first_name}}</code>, " +
      "<code>{{last_name}}</code> and <code>{{phone}}</code>.</p>" +
      "<p>Bird caps messages at 12 segments. Longer content is rejected rather than silently " +
      "truncated. Use Preview to check segments before sending.</p>",
  },
  bird_audience: {
    title: "Bird audience",
    body: "<p>Contacts are snapshotted into the campaign when it is saved. Editing a list later " +
      "does not change an existing campaign. Duplicate phone numbers are collapsed, and suppressed " +
      "or unsubscribed contacts are not sent.</p>",
  },
  bird_audience_lists: {
    title: "Bird audience lists",
    body: "<p>Attach saved Bird SMS lists to include all their contacts. The member count shows " +
      "the current list size. Contacts are copied into the campaign, so the campaign keeps its " +
      "original audience if the list changes later.</p>",
  },
  bird_extra_phones: {
    title: "Additional phone numbers",
    body: "<p>Add recipients that are not in a saved list. Enter one phone number per line or " +
      "separate them with commas. Use international E.164 format when possible.</p>" +
      "<p><em>Example:</em> <code>+15551234567, +15557654321</code></p>",
  },
  bird_batch_size: {
    title: "Bird batch size",
    body: "<p>The maximum number of messages claimed in one worker cycle. Smaller batches react " +
      "faster to pause/cancel and stay below provider limits; larger batches reduce overhead. " +
      "The configured rate and daily caps can still make the effective batch smaller.</p>",
  },
  bird_rate: {
    title: "Bird messages per minute",
    body: "<p>The maximum send rate for this campaign, shared with the global Bird SMS budget. " +
      "The worker will not exceed this rate even when more messages are queued. Lower it while " +
      "warming up a new sender or number.</p>",
  },
  bird_stagger: {
    title: "Stagger within batch",
    body: "<p>Adds a random delay of up to this many seconds between messages inside a batch. " +
      "This avoids a perfectly regular sending pattern and spreads delivery load.</p>" +
      "<p>Set to <code>0</code> for no extra delay. The per-minute rate is still enforced.</p>",
  },
  bird_caps: {
    title: "Bird hourly and daily caps",
    body: "<p>Hard limits on recipients per hour and per day for Bird SMS. Sends wait or defer " +
      "when a cap is reached; they are not discarded. Keep these below the limits agreed with " +
      "Bird or your local regulations.</p>",
  },
  bird_soft_bounce: {
    title: "Bird soft-bounce policy",
    body: "<p>Controls what happens after a temporary delivery failure, such as a full mailbox " +
      "or unavailable handset.</p><p><strong>Threshold</strong> suppresses after the configured " +
      "number of soft bounces, <strong>first bounce</strong> suppresses immediately, and " +
      "<strong>never</strong> leaves the contact active.</p>",
  },
  bird_soft_bounce_threshold: {
    title: "Bird soft-bounce threshold",
    body: "<p>The number of soft bounces allowed before the contact is suppressed when the policy " +
      "is <strong>Suppress after threshold</strong>. A lower number protects reputation but may " +
      "drop recipients whose phone was temporarily unreachable.</p>",
  },
  bird_metrics: {
    title: "Bird campaign metrics",
    body: "<p><strong>Recipients</strong> is the campaign snapshot. <strong>Sent</strong> counts " +
      "requests accepted by Bird. <strong>Delivered</strong> comes from signed delivery webhooks. " +
      "<strong>Failed / undelivered</strong> combines hard failures and provider undelivered events.</p>",
  },
  bird_recipient_status: {
    title: "Bird recipient status",
    body: "<p>The delivery state for one recipient: queued, sending, sent, delivered, failed, " +
      "undelivered, hard/soft bounce, blacklisted, unsubscribed or skipped. Statuses update from " +
      "Bird webhooks and may arrive out of order; the newest provider timestamp wins.</p>",
  },
  bird_segments: {
    title: "SMS segments",
    body: "<p>The number of billable SMS parts. GSM-7 allows 160 characters in one segment and " +
      "153 per part when concatenated; UCS-2 allows 70 and 67. Bird rejects messages over " +
      "12 segments.</p>",
  },
  bird_circuit_breaker: {
    title: "Bird circuit breaker",
    body: "<p>Automatically blocks sending when failure signals become unsafe, such as a high " +
      "hard-bounce rate. <strong>Warning</strong> still sends; <strong>tripped/blocked</strong> " +
      "stops new sends until the issue is fixed and the circuit recovers.</p>",
  },
  bird_test_sms: {
    title: "Send test SMS",
    body: "<p>Sends one real message through Bird to a number you choose. It uses the campaign " +
      "copy, sender, category and encoding, so it consumes wallet credit and can trigger STOP " +
      "handling. Use a number you control.</p>",
  },
  bird_controls: {
    title: "Bird campaign controls",
    body: "<p><strong>Start</strong> queues a draft or scheduled campaign. <strong>Pause</strong> " +
      "stops new sends and keeps the queue. <strong>Resume</strong> continues it. " +
      "<strong>Cancel</strong> stops the campaign and cannot be undone; queued recipients are not sent.</p>",
  },
  bird_reuse_audience: {
    title: "Reuse Bird audience",
    body: "<p><strong>New</strong> clones only contacts never messaged by this campaign. " +
      "<strong>Previous</strong> clones only this campaign&rsquo;s recipients. <strong>All</strong> " +
      "combines both. Suppressed and unsubscribed contacts stay excluded.</p>",
  },
  bird_overview_contacts: {
    title: "Bird contacts",
    body: "<p>The number of contacts stored in the isolated Bird SMS workspace. These are " +
      "separate from email contacts and Brevo B SMS contacts.</p>",
  },
  bird_overview_sendable: {
    title: "Sendable Bird contacts",
    body: "<p>Contacts whose status is active and whose number is not suppressed. Use this as " +
      "the realistic audience size before creating a campaign.</p>",
  },
  bird_overview_lists: {
    title: "Bird lists",
    body: "<p>Saved groups of Bird SMS contacts. A campaign can attach one or more lists; its " +
      "audience is snapshotted when saved, so later list edits do not affect an existing campaign.</p>",
  },
  bird_overview_sending: {
    title: "Sending now",
    body: "<p>The number of Bird campaigns currently in the <code>sending</code> state. High " +
      "priority campaigns take the shared send budget; other active campaigns wait their turn.</p>",
  },
  bird_spend: {
    title: "Today&rsquo;s Bird spend",
    body: "<p>Estimated spend for today, calculated from delivery cost webhooks and local segment " +
      "estimates. It is not a wallet balance. Bird wallet balances require an organization OAuth " +
      "token with <code>org:billing:read</code>, which this SMS workspace does not use.</p>",
  },
  bird_daily_budget: {
    title: "Bird daily budget",
    body: "<p>The local daily spend guardrail. New sends stop when estimated spend reaches this " +
      "amount. It is not enforced by Bird and does not reflect your real wallet balance.</p>",
  },
  bird_remaining_budget: {
    title: "Bird remaining budget",
    body: "<p>The local daily budget minus today&rsquo;s estimated spend. A dash means the remaining " +
      "amount cannot be calculated yet, usually because no spend has been recorded.</p>",
  },
  bird_rate_caps: {
    title: "Bird rate and caps",
    body: "<p>The global per-minute rate, hourly recipient cap and daily recipient cap applied " +
      "to all Bird SMS campaigns. Campaign-level values cannot exceed these guards.</p>",
  },
  bird_campaign_status: {
    title: "Bird campaign statuses",
    body: "<p>Draft is not started, scheduled is waiting, sending is active, paused keeps its " +
      "queue, and completed/cancelled are terminal. Held means the worker stopped the campaign " +
      "for a safety reason; fix the cause before resuming.</p>",
  },
  bird_isolation: {
    title: "Bird SMS isolation",
    body: "<p>Bird contacts, templates, campaigns and delivery events live in dedicated " +
      "<code>bird_sms_*</code> tables. Nothing here reads or writes Brevo B SMS or email data, " +
      "so problems in one channel cannot corrupt the others.</p>",
  },
  bird_import: {
    title: "Import Bird contacts",
    body: "<p>Upload a CSV or Excel file, map its columns, then import. Imported phone numbers " +
      "are normalised and duplicates are skipped. Existing contacts are updated rather than " +
      "duplicated when the phone number matches.</p>",
  },
  bird_import_file: {
    title: "CSV or Excel file",
    body: "<p>Choose a <code>.csv</code>, <code>.xlsx</code> or <code>.xls</code> file. The first " +
      "row should contain column names. The file is read locally in your browser before mapped " +
      "rows are sent to the server.</p>",
  },
  bird_column_mapping: {
    title: "Bird column mapping",
    body: "<p>Tell the importer which file column contains each field. <strong>Phone</strong> is " +
      "required; first name, last name and country are optional. Unmapped columns are ignored.</p>",
  },
  bird_phone: {
    title: "Bird phone number",
    body: "<p>The recipient&rsquo;s mobile number. Use E.164 international format for the most " +
      "reliable routing: a <code>+</code>, country code, then the national number without spaces.</p>" +
      "<p><em>Example:</em> <code>+15551234567</code></p>",
  },
  bird_first_name: {
    title: "Bird first name",
    body: "<p>Optional. Used by the <code>{{first_name}}</code> merge tag and shown in contact " +
      "tables. It does not affect delivery.</p>",
  },
  bird_last_name: {
    title: "Bird last name",
    body: "<p>Optional. Used by the <code>{{last_name}}</code> merge tag and shown in contact " +
      "tables. It does not affect delivery.</p>",
  },
  bird_country: {
    title: "Bird contact country",
    body: "<p>Optional ISO country code used when normalising national phone numbers. If the " +
      "number already starts with <code>+</code>, the number&rsquo;s own country code wins.</p>" +
      "<p><em>Example:</em> <code>GB</code> or <code>US</code></p>",
  },
  bird_contact_status: {
    title: "Bird contact status",
    body: "<p>Active contacts can receive messages. Unsubscribed, blacklisted and deleted contacts " +
      "are suppressed from future sends. Unsubscribed and blacklisted are not the same as deleted: " +
      "deleted rows can be restored.</p>",
  },
  bird_delivery_status: {
    title: "Bird delivery status",
    body: "<p>The latest delivery event for this contact across all Bird campaigns, such as sent, " +
      "delivered, bounced, failed or unsubscribed. It helps spot bad numbers before a new send.</p>",
  },
  bird_contact_lists: {
    title: "Bird contact lists",
    body: "<p>Lists group Bird contacts for reuse. A contact can belong to several lists. Deleting " +
      "a list does not delete its contacts or change campaigns that already snapshotted them.</p>",
  },
  bird_list_name: {
    title: "Bird list name",
    body: "<p>A label for the list. Choose something specific such as <code>UK leads &mdash; October</code> " +
      "so operators know which audience they are selecting.</p>",
  },
  bird_list_description: {
    title: "Bird list description",
    body: "<p>Optional internal notes about where the list came from, its consent basis or when " +
      "it should be used. Recipients do not see it.</p>",
  },
  bird_list_phones: {
    title: "Add list members by phone",
    body: "<p>Enter phone numbers to add while creating a list. Separate them with commas or " +
      "new lines. Existing contacts are reused; unknown numbers are created.</p>",
  },
  bird_restore: {
    title: "Restore Bird contact",
    body: "<p>Removes the soft-delete flag from a contact without changing its subscription " +
      "status. Suppressed or unsubscribed contacts remain suppressed; restoring does not grant " +
      "permission to send.</p>",
  },
  bird_saved_templates: {
    title: "Saved Bird templates",
    body: "<p>Reusable message bodies stored in this app. They are separate from Bird&rsquo;s own " +
      "template catalogue. Saved templates can be edited or copied into campaigns.</p>",
  },
  bird_report_filters: {
    title: "Bird report filters",
    body: "<p>Filter recipients by delivery category such as delivered, undelivered, failed or " +
      "bounced. The count beside each filter is the number of recipients in that category.</p>",
  },
  bird_report_columns: {
    title: "Bird report columns",
    body: "<p>Phone and name identify the recipient. Contact status is the opt-out state; " +
      "recipient status is this campaign&rsquo;s delivery state. Segments and cost are billable " +
      "usage. Error fields contain Bird&rsquo;s diagnostic codes.</p>",
  },
  bird_report_csv: {
    title: "Download Bird report CSV",
    body: "<p>Downloads the currently filtered recipient rows as a CSV file for accounting or " +
      "analysis. The export uses the same columns shown on this page.</p>",
  },
  bird_collision_window: {
    title: "Bird collision window",
    body: "<p>The minimum time window used by the pacer to avoid sending two Bird messages at the " +
      "same moment. Raise it to spread traffic; lower it only if you understand the provider rate " +
      "limits.</p>",
  },
  bird_cross_gap: {
    title: "Bird cross-campaign gap",
    body: "<p>A random pause inserted between campaigns that share the Bird sending queue. It " +
      "prevents two campaigns from firing back-to-back and looking like a burst. Set min and max " +
      "in seconds.</p>",
  },
  bird_pause: {
    title: "Pause all Bird SMS sending",
    body: "<p>A Bird-only kill switch. When enabled, every Bird campaign stops starting new sends " +
      "within seconds. Email and Brevo B SMS are not affected. Existing queued messages remain " +
      "until you turn this off.</p>",
  },
  bird_warmup: {
    title: "Bird warm-up",
    body: "<p>Gradually increases sending volume from the warm-up start date. This is useful for " +
      "a new number or sender with little history. Warm-up reduces the effective rate; it does " +
      "not change the configured caps themselves.</p>",
  },
  bird_warmup_start: {
    title: "Bird warm-up start date",
    body: "<p>The date the warm-up ramp begins. The app calculates how far into the ramp you are " +
      "and applies a reduced send factor. Choose the date you actually started using the number, " +
      "not a future date.</p>",
  },
  bird_cost_segment: {
    title: "Estimated cost per segment",
    body: "<p>Local estimate in USD used for spend tracking and budget warnings. It does not " +
      "change Bird&rsquo;s actual price. Check your Bird rate card and update this if your rate " +
      "differs.</p><p><em>Example:</em> <code>0.008</code></p>",
  },
  bird_low_budget: {
    title: "Low budget warning",
    body: "<p>Shows a warning on the Bird overview when the remaining local daily budget falls " +
      "below this amount. It is a visual warning only; the hard stop is the daily budget itself.</p>",
  },
  bird_bounce_threshold: {
    title: "Bird max hard-bounce rate",
    body: "<p>Fraction of sent messages allowed to hard-bounce before the circuit breaker pauses " +
      "Bird sending. <code>0.03</code> means 3%. Keep it low while warming up a new sender.</p>",
  },
  bird_telegram_override: {
    title: "Bird Telegram overrides",
    body: "<p>Optional per-channel bot token and chat ID. When set they override the email/B SMS " +
      "Telegram settings for Bird alerts only. Leave both blank to use the shared Telegram " +
      "configuration. Clear removes the Bird-specific overrides.</p>",
  },
  bird_webhook_requirements: {
    title: "Bird webhook requirements",
    body: "<p>Register <code>&lt;public base URL&gt;/api/webhooks/bird</code> in Bird and copy its " +
      "signing secret here. Bird sends Standard Webhooks headers; the app verifies the HMAC, " +
      "rejects timestamps older than five minutes, and deduplicates repeated events by webhook ID.</p>",
  },
  public_base_url: {
    title: "Public base URL",
    body: "<p>The public https address used for the open-tracking pixel and the unsubscribe link.</p>" +
      "<p><strong>Must be reachable from the internet — localhost will break unsubscribe " +
      "(a spam signal) and makes your tracking link dead.</strong></p>" +
      "<p><em>Example:</em> <code>https://mail.yourdomain.com</code></p>",
  },
  max_hard_bounce_rate: {
    title: "Max hard-bounce rate",
    body: "<p>Fraction (0.02 = 2%). If a campaign's hard bounces exceed this, the circuit " +
      "breaker pauses it automatically. Keep it below ~0.03 while warming up.</p>",
  },
  max_complaint_rate: {
    title: "Max complaint rate",
    body: "<p>Fraction (0.001 = 0.1%). Spam-mark complaints above this pause the campaign. " +
      "RealWorld Gmail guidance is to stay under 0.1%.</p>",
  },
  sending_paused: {
    title: "Pause all sending",
    body: "<p>A global kill switch. When on, <strong>every</strong> campaign stops within " +
      "seconds and no new sends are allowed until you turn it back off. Useful if something " +
      "looks wrong.</p>",
  },
  send_rate_per_minute: {
    title: "Send rate (emails / minute)",
    body: "<p>The maximum number of emails sent per minute, <strong>shared across all " +
      "campaigns</strong>. This is the single most important control for protecting your " +
      "sender reputation.</p>" +
      "<p>Start low with a new domain and ramp up slowly (warm-up):</p>" +
      "<ul><li><strong>Safe</strong> — 30/min</li><li><strong>Balanced</strong> — 120/min</li>" +
      "<li><strong>Fast</strong> — 600/min (only for warmed, high-reputation domains)</li></ul>",
  },
  max_recipients_per_hour: {
    title: "Max recipients per hour",
    body: "<p>A hard ceiling on emails sent per hour, across all campaigns. Caps protect you " +
      "from runaway jobs. <em>Example:</em> <code>2000</code></p>",
  },
  max_recipients_per_day: {
    title: "Max recipients per day",
    body: "<p>A hard ceiling on emails sent per day, across all campaigns. Keep it within your " +
      "providers's expectations. <em>Example:</em> <code>8000</code></p>",
  },
  default_batch_size: {
    title: "Batch size",
    body: "<p>How many recipients are claimed and processed per cycle. Larger batches finish " +
      "faster but check the rate/caps less often. <code>500</code> is a good default.</p>",
  },
  stagger_seconds: {
    title: "Stagger jitter (seconds)",
    body: "<p>Adds a random 0–N second delay <strong>between every email</strong>, not just " +
      "between batches, so traffic doesn't look like an automated burst.</p>" +
      "<p>The delay is honoured exactly: if it is longer than a single work cycle the next " +
      "email is simply scheduled later — pacing never blocks the sender and never skips a " +
      "recipient.</p>" +
      "<p><em>Example:</em> <code>50</code> waits a random 0–50s before each email.</p>",
  },
  window: {
    title: "Delivery window",
    body: "<p>Only send during certain hours of the day in the chosen timezone — helps avoid " +
      "late-night sends and looks more human.</p>" +
      "<p><em>Example:</em> <code>08:00</code> to <code>18:00</code>, " +
      "<code>America/New_York</code>. Leave blank for 24/7.</p>",
  },
  warmup: {
    title: "Warm-up mode",
    body: "<p>Gently ramps volume on a new domain so mailbox providers learn you send wanted " +
      "mail. Keep the send rate low and increase gradually over 2–4 weeks.</p>",
  },
  soft_bounce: {
    title: "Soft-bounce handling",
    body: "<p>A <strong>soft bounce</strong> is a temporary failure (full mailbox, greylisting, " +
      "server busy). Choose how this campaign treats the contact:</p>" +
      "<ul><li><strong>Suppress immediately</strong> — safest for reputation (default).</li>" +
      "<li><strong>Suppress after N</strong> — tolerate a few transient failures before " +
      "suppressing; set N with the threshold field.</li>" +
      "<li><strong>Never suppress</strong> — always keep the address active (use with care).</li></ul>" +
      "<p>Spam complaints and unsubscribes are always suppressed.</p>",
  },
  hard_bounce: {
    title: "Hard-bounce handling",
    body: "<p>A <strong>hard bounce</strong> is permanent (unknown mailbox or domain). " +
      "Choose how this campaign treats the contact:</p>" +
      "<ul><li><strong>Suppress immediately</strong> — fastest reputation protection (default).</li>" +
      "<li><strong>Suppress after N</strong> — count hard bounces on the contact and suppress " +
      "when the tally reaches N; set N with the threshold field.</li>" +
      "<li><strong>Never suppress</strong> — keep the address active (auditing only; risky).</li></ul>" +
      "<p>When a campaign is paused by the circuit breaker, use <strong>Restart sending</strong> " +
      "to continue with the remaining queue: contacts already emailed (including the bounced " +
      "one) are never re-sent.</p>",
  },
  lists: {
    title: "Contact lists",
    body: "<p>Reusable groups of contacts. A campaign can target any number of lists, " +
      "and a contact can be in many lists.</p>" +
      "<p>Selected lists are merged together (duplicates removed) when the campaign is built.</p>",
  },
  timezone: {
    title: "Contact timezone",
    body: "<p>Used only when a campaign restricts delivery to a time window for each contact. " +
      "Leave it as <code>UTC</code> unless you rely on per-contact local-time sending.</p>",
  },
  brevo_plan: {
    title: "Your Brevo plan",
    body: "<p>Tell webmailer which Brevo plan you are on. It sets the <strong>recommended</strong> " +
      "rate and caps (prefilled above) and the <strong>hard limits</strong> the settings guard " +
      "enforces.</p><p>Values above the recommended level show a warning; values above the plan " +
      "limit are rejected so a typo can't damage your sender reputation.</p>",
  },
  priority: {
    title: "High priority",
    body: "<p>While a high-priority campaign still has recipients to send, it takes the whole " +
      "send budget and other active campaigns wait (without being paused). They resume " +
      "automatically once it finishes.</p><p>Only <strong>one</strong> campaign can be high " +
      "priority at a time.</p>",
  },
};

let lastInfoTrigger = null;

function openInfo(key, trigger) {
  const entry = INFO[key];
  const modal = document.getElementById("info-modal");
  if (!entry || !modal) return;
  lastInfoTrigger = trigger || document.activeElement;
  document.getElementById("info-title").textContent = entry.title;
  document.getElementById("info-body").innerHTML = entry.body;
  modal.hidden = false;
  const close = modal.querySelector(".modal-close");
  if (close) close.focus();
}

function closeInfo() {
  const modal = document.getElementById("info-modal");
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  if (lastInfoTrigger && document.contains(lastInfoTrigger)) {
    lastInfoTrigger.focus();
  }
}

function initInfoModals() {
  const modal = document.getElementById("info-modal");
  if (!modal) return;
  document.querySelectorAll("[data-info]").forEach((trigger) => {
    const entry = INFO[trigger.dataset.info];
    if (!trigger.getAttribute("aria-label")) {
      trigger.setAttribute(
        "aria-label",
        "More information about " + (entry ? entry.title : trigger.dataset.info)
      );
    }
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-controls", "info-modal");
  });
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-info]");
    if (trigger) {
      event.preventDefault();
      event.stopPropagation();
      openInfo(trigger.dataset.info, trigger);
      return;
    }
    if (event.target.closest("[data-close-info]")) {
      closeInfo();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeInfo();
  });
}

// ------------------------------------------------------------------ //
// Campaign editor (new + edit) with rich-text toggle
// ------------------------------------------------------------------ //
function initCampaignEditor() {
  const form = document.getElementById("campaign-form");
  if (!form) return;

  const textarea = document.getElementById("html_content");
  const toggle = document.getElementById("rich-toggle");
  const quillHost = document.getElementById("quill-editor");
  const htmlLabel = document.getElementById("html-label");
  const campaignId = form.dataset.campaignId || null;
  const currentPriority = form.dataset.currentPriority || "normal";
  const priorityInput = form.querySelector("[name=priority]");
  let quill = null;

  function ensureQuill() {
    if (quill || !quillHost || typeof Quill === "undefined") return quill;
    quill = new Quill(quillHost, {
      theme: "snow",
      modules: { toolbar: [
        [{ header: [1, 2, 3, false] }],
        ["bold", "italic", "underline", "strike"],
        [{ list: "ordered" }, { list: "bullet" }],
        ["blockquote", "link"],
        [{ color: [] }, { background: [] }],
        ["clean"],
      ] },
    });
    quill.clipboard.dangerouslyPasteHTML(textarea.value || "");
    return quill;
  }

  if (toggle) {
    toggle.addEventListener("change", () => {
      const rich = toggle.checked;
      if (rich) {
        ensureQuill();
        quillHost.hidden = false;
        htmlLabel.hidden = true;
      } else {
        if (quill) textarea.value = quill.root.innerHTML;
        quillHost.hidden = true;
        htmlLabel.hidden = false;
      }
    });
  }

  document.querySelectorAll("[data-merge]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tag = btn.dataset.merge;
      if (toggle && toggle.checked && ensureQuill()) {
        const range = quill.getSelection(true);
        quill.insertText(range ? range.index : quill.getLength(), tag);
      } else {
        const start = textarea.selectionStart ?? textarea.value.length;
        const end = textarea.selectionEnd ?? textarea.value.length;
        textarea.value = textarea.value.slice(0, start) + tag + textarea.value.slice(end);
        textarea.focus();
        textarea.selectionStart = textarea.selectionEnd = start + tag.length;
      }
    });
  });

  form.querySelectorAll("[data-rate-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = form.querySelector("[name=send_rate_per_minute]");
      if (input) input.value = btn.dataset.ratePreset;
    });
  });

  const templateSelect = document.getElementById("template-select");
  const templatesDataEl = document.getElementById("templates-data");
  if (templateSelect && templatesDataEl) {
    let tplCache = [];
    try { tplCache = JSON.parse(templatesDataEl.textContent) || []; } catch (_) { tplCache = []; }
    templateSelect.addEventListener("change", () => {
      const entry = tplCache.find((t) => t.id === templateSelect.value);
      if (!entry) return;
      const hasContent = (toggle && toggle.checked && quill)
        ? quill.getText().trim() : (textarea.value || "").trim();
      if (hasContent &&
          !window.confirm("Replace the current subject and body with this template?")) {
        templateSelect.value = "";
        return;
      }
      const subjectInput = form.querySelector("[name=subject]");
      if (subjectInput && entry.subject) subjectInput.value = entry.subject;
      const html = entry.html_content || "";
      textarea.value = html;
      if (toggle && toggle.checked && ensureQuill()) quill.clipboard.dangerouslyPasteHTML(html);
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const fd = new FormData(form);

    let html = fd.get("html_content") || "";
    if (toggle && toggle.checked && quill) html = quill.root.innerHTML;
    if (!html.trim()) { setStatus("form-status", "Email content is required.", true); return; }

    const plain = stripHtml(html);
    const payload = {
      name: (fd.get("name") || "").trim(),
      subject: (fd.get("subject") || "").trim(),
      from_email: (fd.get("from_email") || "").trim(),
      from_name: (fd.get("from_name") || "").trim(),
      html_content: html,
      plain_text_content: plain,
    };

    // Strategic settings — only send what the user explicitly filled in so
    // blank fields inherit the global defaults (create) / keep current (edit).
    const numField = (name) => {
      const raw = (fd.get(name) || "").toString().trim();
      if (raw === "") return null;
      const n = Number(raw);
      return Number.isNaN(n) ? null : n;
    };
    ["batch_size", "send_rate_per_minute", "stagger_seconds"].forEach((k) => {
      const v = numField(k);
      if (v !== null) payload[k] = v;
    });
    const opt = (name) => ((fd.get(name) || "").toString().trim() || null);
    const tz = opt("window_timezone");
    if (tz) payload.window_timezone = tz;
    const sched = opt("schedule_at");
    if (sched) payload.schedule_at = sched;
    const ws = opt("window_start");
    if (ws) payload.window_start = ws;
    const we = opt("window_end");
    if (we) payload.window_end = we;
    const sbp = opt("soft_bounce_policy");
    if (sbp) payload.soft_bounce_policy = sbp;
    const sbt = numField("soft_bounce_threshold");
    if (sbt !== null) payload.soft_bounce_threshold = sbt;
    const hbp = opt("hard_bounce_policy");
    if (hbp) payload.hard_bounce_policy = hbp;
    const hbt = numField("hard_bounce_threshold");
    if (hbt !== null) payload.hard_bounce_threshold = hbt;

    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    setStatus("form-status", "Saving...", false);
    try {
      const selectedLists = Array.from(form.querySelectorAll(".list-picker-item:checked"))
        .map((el) => el.value);
      if (campaignId) {
        await postJSON("/api/campaigns/" + campaignId, payload, "PATCH");
        if (selectedLists.length || form.querySelector("[data-list-picker]")) {
          await postJSON("/api/campaigns/" + campaignId + "/lists",
            { list_ids: selectedLists, attach: true });
        }
        if (priorityInput) {
          const want = priorityInput.checked ? "high" : "normal";
          if (want !== currentPriority) {
            await postJSON("/api/campaigns/" + campaignId + "/priority", { high: want === "high" });
          }
        }
        window.location.href = "/campaigns/" + campaignId;
      } else {
        const emailsRaw = (fd.get("recipient_emails") || "").trim();
        if (emailsRaw) {
          payload.recipient_emails = emailsRaw.split(/[,\n;]+/).map((s) => s.trim()).filter(Boolean);
        }
        if (selectedLists.length) payload.list_ids = selectedLists;
        if (priorityInput && priorityInput.checked) payload.priority = "high";
        const data = await postJSON("/api/campaigns", payload);
        window.location.href = "/campaigns/" + data.campaign.id;
      }
    } catch (err) {
      setStatus("form-status", "Error: " + err.message, true);
      button.disabled = false;
    }
  });

  const preflight = document.getElementById("preflight-btn");
  const preflightOut = document.getElementById("preflight-result");
  if (preflight) {
    preflight.addEventListener("click", async () => {
      const html = (toggle && toggle.checked && quill) ? quill.root.innerHTML : (textarea.value || "");
      const subject = form.querySelector("[name=subject]").value || "";
      setStatus(preflightOut, "Checking...", false);
      try {
        const r = await postJSON("/api/preflight", { html_content: html, subject, links: [] });
        setStatus(preflightOut, "Spam score: " + r.score + " (" + r.verdict + ")" +
          (r.issues && r.issues.length ? " — " + r.issues.join("; ") : ""), r.verdict === "risky");
      } catch (err) {
        setStatus(preflightOut, "Preflight failed: " + err.message, true);
      }
    });
  }
}

// ------------------------------------------------------------------ //
// Campaign settings (pacing / schedule / delivery window)
// ------------------------------------------------------------------ //
function initSettingsForm() {
  const form = document.getElementById("settings-form");
  if (!form) return;
  const campaignId = form.dataset.campaignId;

  form.querySelectorAll("[data-rate-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = form.querySelector("[name=send_rate_per_minute]");
      if (input) input.value = btn.dataset.ratePreset;
    });
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const fd = new FormData(form);
    const num = (k) => (fd.get(k) === "" ? null : Number(fd.get(k)));
    const payload = {
      batch_size: num("batch_size"),
      send_rate_per_minute: num("send_rate_per_minute"),
      stagger_seconds: num("stagger_seconds"),
      window_timezone: (fd.get("window_timezone") || "UTC").trim(),
    };
    const scheduleAt = (fd.get("schedule_at") || "").trim();
    if (scheduleAt) payload.schedule_at = scheduleAt;
    const ws = (fd.get("window_start") || "").trim();
    const we = (fd.get("window_end") || "").trim();
    if (ws) payload.window_start = ws;
    if (we) payload.window_end = we;
    const sbp = (fd.get("soft_bounce_policy") || "").trim();
    if (sbp) payload.soft_bounce_policy = sbp;
    payload.soft_bounce_threshold = num("soft_bounce_threshold");
    const hbp = (fd.get("hard_bounce_policy") || "").trim();
    if (hbp) payload.hard_bounce_policy = hbp;
    payload.hard_bounce_threshold = num("hard_bounce_threshold");
    Object.keys(payload).forEach((k) => payload[k] == null && delete payload[k]);

    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    setStatus("form-status", "Saving...", false);
    try {
      await postJSON("/api/campaigns/" + campaignId, payload, "PATCH");
      window.location.href = "/campaigns/" + campaignId;
    } catch (err) {
      setStatus("form-status", "Error: " + err.message, true);
      button.disabled = false;
    }
  });
}

// ------------------------------------------------------------------ //
// Contact import with column mapping
// ------------------------------------------------------------------ //
function initImport() {
  const panel = document.getElementById("import-panel");
  const toggle = document.getElementById("toggle-import");
  if (toggle && panel) {
    toggle.addEventListener("click", () => { panel.hidden = !panel.hidden; });
  }

  const fileInput = document.getElementById("import-file");
  if (!fileInput) return;

  const previewBtn = document.getElementById("preview-btn");
  const importBtn = document.getElementById("import-btn");
  const mappingSection = document.getElementById("mapping-section");
  const fieldSelect = (name) => document.getElementById("map-" + name);
  let columns = [];
  let rawRows = [];

  function fillSelect(select, selected) {
    select.innerHTML = '<option value="">— none —</option>' + columns.map(
      (c) => `<option value="${escapeHtml(c)}"${c === selected ? " selected" : ""}>${escapeHtml(c)}</option>`
    ).join("");
  }

  function currentMapping() {
    return {
      email: fieldSelect("email").value,
      first_name: fieldSelect("first_name").value,
      last_name: fieldSelect("last_name").value,
      timezone: fieldSelect("timezone").value,
    };
  }

  function customColumns() {
    return Array.from(document.querySelectorAll(".custom-col:checked")).map((c) => c.value);
  }

  function renderCustomFields(suggested) {
    const used = new Set(Object.values(suggested));
    const host = document.getElementById("custom-fields");
    host.innerHTML = columns.filter((c) => !used.has(c)).map(
      (c) => `<label class="checkbox"><input type="checkbox" class="custom-col" value="${escapeHtml(c)}"> ${escapeHtml(c)}</label>`
    ).join("") || '<span class="muted">No extra columns.</span>';
  }

  function renderStaging() {
    const host = document.getElementById("staging-table");
    if (!rawRows.length) { host.innerHTML = ""; return; }
    const map = currentMapping();
    const custom = customColumns();
    const headers = ["email", "first_name", "last_name", ...custom];
    const head = "<tr>" + headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("") + "</tr>";
    const body = rawRows.map((row, i) => {
      const cells = headers.map((h) => {
        const derived = ["email", "first_name", "last_name"].includes(h);
        const source = derived ? map[h] : h;
        const value = source ? (row[source] ?? "") : "";
        const field = derived ? h : "meta";
        return `<td><input class="stage-cell" data-row="${i}" data-field="${escapeHtml(field)}"` +
          ` data-col="${escapeHtml(h)}" value="${escapeHtml(value)}"></td>`;
      }).join("");
      return `<tr>${cells}</tr>`;
    }).join("");
    host.innerHTML = `<div class="stage-scroll"><table class="table"><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
  }

  previewBtn.addEventListener("click", async () => {
    if (!fileInput.files.length) { setStatus("form-status", "Choose a file first.", true); return; }
    setStatus("form-status", "Reading file...", false);
    const body = new FormData();
    body.append("file", fileInput.files[0]);
    try {
      const res = await fetch("/api/contacts/import/preview", {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken() },
        body,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(JSON.stringify(data.error || res.statusText));
      columns = data.columns || [];
      rawRows = data.rows || [];
      const suggested = data.suggested_mapping || {};
      ["email", "first_name", "last_name", "timezone"].forEach((f) => fillSelect(fieldSelect(f), suggested[f]));
      renderCustomFields(suggested);
      renderStaging();
      mappingSection.hidden = false;
      const note = data.truncated ? ` (showing first ${rawRows.length})` : "";
      setStatus("form-status", `${data.total_rows} row(s), ${columns.length} columns${note}.`, false);
    } catch (err) {
      setStatus("form-status", "Error: " + err.message, true);
    }
  });

  document.querySelectorAll(".map-select").forEach((sel) =>
    sel.addEventListener("change", renderStaging));
  document.getElementById("custom-fields").addEventListener("change", renderStaging);

  importBtn.addEventListener("click", async () => {
    const map = currentMapping();
    if (!map.email) { setStatus("import-status", "Map an email column.", true); return; }

    const rowsByIndex = {};
    document.querySelectorAll(".stage-cell").forEach((cell) => {
      const i = cell.dataset.row;
      rowsByIndex[i] = rowsByIndex[i] || { email: "", first_name: "", last_name: "", metadata: {} };
      const row = rowsByIndex[i];
      const val = cell.value.trim();
      if (cell.dataset.field === "meta") { if (val) row.metadata[cell.dataset.col] = val; }
      else row[cell.dataset.field] = val || null;
    });
    const staged = Object.values(rowsByIndex);
    const rows = staged.filter((r) => r.email);
    const skipped = staged.length - rows.length;
    if (!rows.length) { setStatus("import-status", "No rows with an email.", true); return; }

    const target = (document.querySelector('input[name="import-target"]:checked') || {}).value || "db";
    const payload = { rows };
    if (target === "existing") {
      payload.list_id = document.getElementById("import-target-list").value || null;
      if (!payload.list_id) { setStatus("import-status", "Pick a list.", true); return; }
    } else if (target === "new") {
      const name = document.getElementById("import-target-name").value.trim();
      if (!name) { setStatus("import-status", "Name the new list.", true); return; }
      payload.list_name = name;
    }

    importBtn.disabled = true;
    setStatus("import-status", "Saving...", false);
    try {
      const data = await postJSON("/api/contacts/import/commit", payload);
      let msg = `Saved ${data.contacts} contact(s)`;
      if (data.added_to_list) msg += `, added ${data.added_to_list} to the list`;
      if (skipped) msg += ` (${skipped} row(s) without email skipped)`;
      setStatus("import-status", msg + ".", false);
      setTimeout(() => window.location.reload(), 900);
    } catch (err) {
      setStatus("import-status", "Error: " + err.message, true);
      importBtn.disabled = false;
    }
  });
}

// ------------------------------------------------------------------ //
// Contacts hub: inline edit, multi-select, add-to-list
// ------------------------------------------------------------------ //
function initContactsHub() {
  const table = document.getElementById("contacts-table");
  if (!table) return;

  const selectAll = document.getElementById("select-all");
  const countEl = document.getElementById("selected-count");
  const selectedIds = () =>
    Array.from(document.querySelectorAll(".contact-select:checked")).map((c) => c.value);

  function refreshCount() {
    if (countEl) countEl.textContent = selectedIds().length + " selected";
  }

  table.addEventListener("change", (event) => {
    if (event.target.classList.contains("contact-select")) refreshCount();
  });
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      document.querySelectorAll(".contact-select").forEach((c) => { c.checked = selectAll.checked; });
      refreshCount();
    });
  }

  table.addEventListener("click", async (event) => {
    const save = event.target.closest("[data-save-contact]");
    if (!save) return;
    const row = save.closest("tr");
    const payload = {};
    row.querySelectorAll(".inline-edit").forEach((input) => {
      payload[input.dataset.field] = input.value.trim();
    });
    save.disabled = true;
    try {
      await postJSON("/api/contacts/" + save.dataset.saveContact, payload, "PATCH");
      save.textContent = "Saved";
      setTimeout(() => { save.textContent = "Save"; }, 1200);
    } catch (err) {
      window.alert("Error: " + err.message);
    } finally {
      save.disabled = false;
    }
  });

  const addBtn = document.getElementById("add-selected-btn");
  if (addBtn) {
    addBtn.addEventListener("click", async () => {
      const ids = selectedIds();
      if (!ids.length) { window.alert("Select contacts first."); return; }
      const listId = document.getElementById("selected-list-target").value;
      if (!listId) { window.alert("Choose a list."); return; }
      try {
        await postJSON("/api/lists/" + listId + "/members", { contact_ids: ids });
        window.location.reload();
      } catch (err) {
        window.alert("Error: " + err.message);
      }
    });
  }

  const createBtn = document.getElementById("create-list-from-selected");
  if (createBtn) {
    createBtn.addEventListener("click", async () => {
      const ids = selectedIds();
      if (!ids.length) { window.alert("Select contacts first."); return; }
      const name = window.prompt("New list name:");
      if (!name || !name.trim()) return;
      try {
        await postJSON("/api/lists", { name: name.trim(), contact_ids: ids });
        window.location.reload();
      } catch (err) {
        window.alert("Error: " + err.message);
      }
    });
  }

  const deleteSelectedBtn = document.getElementById("delete-selected-btn");
  if (deleteSelectedBtn) {
    deleteSelectedBtn.addEventListener("click", async () => {
      const ids = selectedIds();
      if (!ids.length) { window.alert("Select contacts first."); return; }
      if (!window.confirm(
        "Permanently delete " + ids.length + " contact(s)?\n\n" +
        "This is the clean-slate option: the rows (and their suppression state, " +
        "list memberships and campaign recipient records) are removed for good, " +
        "so these addresses become fully reusable. This cannot be undone.\n\n" +
        "To keep their suppression state, use “Hide selected” instead.")) return;
      deleteSelectedBtn.disabled = true;
      try {
        const result = await postJSON("/api/contacts/bulk-delete", { ids });
        window.alert("Deleted " + result.deleted + " contact(s).");
        window.location.reload();
      } catch (err) {
        window.alert("Error: " + err.message);
        deleteSelectedBtn.disabled = false;
      }
    });
  }

  const hideSelectedBtn = document.getElementById("soft-delete-selected-btn");
  if (hideSelectedBtn) {
    hideSelectedBtn.addEventListener("click", async () => {
      const ids = selectedIds();
      if (!ids.length) { window.alert("Select contacts first."); return; }
      if (!window.confirm(
        "Hide " + ids.length + " contact(s)?\n\n" +
        "They are removed from the list but keep their status and suppression " +
        "(e.g. a bounced address stays bounced). You can restore them anytime.")) return;
      hideSelectedBtn.disabled = true;
      try {
        const result = await postJSON("/api/contacts/soft-delete", { ids });
        window.alert("Hidden " + result.soft_deleted + " contact(s).");
        window.location.reload();
      } catch (err) {
        window.alert("Error: " + err.message);
        hideSelectedBtn.disabled = false;
      }
    });
  }

  const restoreSelectedBtn = document.getElementById("restore-selected-btn");
  if (restoreSelectedBtn) {
    restoreSelectedBtn.addEventListener("click", async () => {
      const ids = selectedIds();
      if (!ids.length) { window.alert("Select contacts first."); return; }
      restoreSelectedBtn.disabled = true;
      try {
        const result = await postJSON("/api/contacts/restore", { ids });
        window.alert("Restored " + result.restored + " contact(s).");
        window.location.reload();
      } catch (err) {
        window.alert("Error: " + err.message);
        restoreSelectedBtn.disabled = false;
      }
    });
  }

  table.addEventListener("click", async (event) => {
    const hide = event.target.closest("[data-soft-delete-contact]");
    if (hide) {
      const id = hide.dataset.softDeleteContact;
      if (!window.confirm("Hide this contact? Its status/suppression is kept.")) return;
      hide.disabled = true;
      try {
        await postJSON("/api/contacts/soft-delete", { ids: [id] });
        window.location.reload();
      } catch (err) {
        window.alert("Error: " + err.message);
        hide.disabled = false;
      }
      return;
    }
    const restore = event.target.closest("[data-restore-contact]");
    if (restore) {
      const id = restore.dataset.restoreContact;
      restore.disabled = true;
      try {
        await postJSON("/api/contacts/restore", { ids: [id] });
        window.location.reload();
      } catch (err) {
        window.alert("Error: " + err.message);
        restore.disabled = false;
      }
    }
  });
}

// ------------------------------------------------------------------ //
// Email template gallery: save / edit / delete / preview / reuse
// ------------------------------------------------------------------ //
function initTemplates() {
  const form = document.getElementById("template-form");
  if (!form) return;

  const table = document.getElementById("templates-table");
  const idInput = form.querySelector("[name=id]");
  const nameInput = form.querySelector("[name=name]");
  const subjectInput = form.querySelector("[name=subject]");
  const textarea = form.querySelector("[name=html_content]");
  const toggle = document.getElementById("template-rich-toggle");
  const quillHost = document.getElementById("template-quill");
  const htmlLabel = document.getElementById("template-html-label");
  const title = document.getElementById("template-editor-title");
  const statusId = "template-status";
  const previewWrap = document.getElementById("template-preview");
  const previewFrame = document.getElementById("template-preview-frame");
  let quill = null;

  const dataEl = document.getElementById("templates-data");
  let cache = [];
  try { cache = JSON.parse(dataEl ? dataEl.textContent : "[]") || []; } catch (_) { cache = []; }

  function ensureQuill() {
    if (quill || !quillHost || typeof Quill === "undefined") return quill;
    quill = new Quill(quillHost, {
      theme: "snow",
      modules: { toolbar: [
        [{ header: [1, 2, 3, false] }],
        ["bold", "italic", "underline", "strike"],
        [{ list: "ordered" }, { list: "bullet" }],
        ["blockquote", "link"],
        [{ color: [] }, { background: [] }],
        ["clean"],
      ] },
    });
    quill.clipboard.dangerouslyPasteHTML(textarea.value || "");
    return quill;
  }

  function currentHtml() {
    if (toggle && toggle.checked && quill) return quill.root.innerHTML;
    return textarea.value;
  }

  function setHtml(html) {
    textarea.value = html || "";
    if (quill) quill.clipboard.dangerouslyPasteHTML(html || "");
  }

  function resetForm() {
    form.reset();
    if (idInput) idInput.value = "";
    if (title) title.textContent = "New template";
    if (quill) quill.clipboard.dangerouslyPasteHTML("");
    if (toggle) toggle.checked = false;
    if (quillHost) quillHost.hidden = true;
    if (htmlLabel) htmlLabel.hidden = false;
    if (previewWrap) previewWrap.hidden = true;
    setStatus(statusId, "", false);
  }

  function loadTemplate(id) {
    const entry = cache.find((t) => t.id === id);
    if (!entry) return;
    if (idInput) idInput.value = entry.id;
    if (nameInput) nameInput.value = entry.name || "";
    if (subjectInput) subjectInput.value = entry.subject || "";
    setHtml(entry.html_content || "");
    if (title) title.textContent = "Edit template";
    if (previewWrap) previewWrap.hidden = true;
    setStatus(statusId, "Editing “" + (entry.name || "") + "”.", false);
    form.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  if (toggle) {
    toggle.addEventListener("change", () => {
      if (toggle.checked) {
        ensureQuill();
        if (quill) quill.clipboard.dangerouslyPasteHTML(textarea.value || "");
        quillHost.hidden = false;
        htmlLabel.hidden = true;
      } else {
        if (quill) textarea.value = quill.root.innerHTML;
        quillHost.hidden = true;
        htmlLabel.hidden = false;
      }
    });
  }

  form.querySelectorAll("[data-merge]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tag = btn.dataset.merge;
      if (toggle && toggle.checked && ensureQuill()) {
        const range = quill.getSelection(true);
        quill.insertText(range ? range.index : quill.getLength(), tag);
      } else {
        const start = textarea.selectionStart ?? textarea.value.length;
        const end = textarea.selectionEnd ?? textarea.value.length;
        textarea.value = textarea.value.slice(0, start) + tag + textarea.value.slice(end);
        textarea.focus();
        textarea.selectionStart = textarea.selectionEnd = start + tag.length;
      }
    });
  });

  const linkBtn = document.getElementById("template-link-btn");
  if (linkBtn) {
    linkBtn.addEventListener("click", () => {
      const url = window.prompt("Link URL (https://…):", "https://");
      if (!url || !url.trim()) return;
      const href = url.trim();
      if (toggle && toggle.checked && ensureQuill()) {
        const range = quill.getSelection(true) || { index: quill.getLength(), length: 0 };
        if (!range.length) {
          quill.insertText(range.index, href, "link", href);
        } else {
          quill.formatText(range.index, range.length, "link", href);
        }
      } else {
        const start = textarea.selectionStart ?? textarea.value.length;
        const end = textarea.selectionEnd ?? textarea.value.length;
        const selected = textarea.value.slice(start, end) || href;
        const anchor = '<a href="' + escapeHtml(href) + '">' + escapeHtml(selected) + "</a>";
        textarea.value = textarea.value.slice(0, start) + anchor + textarea.value.slice(end);
        textarea.focus();
        textarea.selectionStart = textarea.selectionEnd = start + anchor.length;
      }
    });
  }

  if (table) {
    table.addEventListener("click", async (event) => {
      const edit = event.target.closest("[data-template-edit]");
      if (edit) { loadTemplate(edit.dataset.templateEdit); return; }
      const del = event.target.closest("[data-template-delete]");
      if (del) {
        const id = del.dataset.templateDelete;
        const row = del.closest("tr");
        const label = row ? (row.querySelector("td") || {}).textContent : "this template";
        if (!window.confirm("Delete template “" + label + "”? This cannot be undone.")) return;
        del.disabled = true;
        try {
          await postJSON("/api/templates/" + id, null, "DELETE");
          if (row) row.remove();
          cache = cache.filter((t) => t.id !== id);
          if (idInput && idInput.value === id) resetForm();
        } catch (err) {
          window.alert("Error: " + err.message);
          del.disabled = false;
        }
      }
    });
  }

  const newBtn = document.getElementById("template-new-btn");
  if (newBtn) newBtn.addEventListener("click", () => { resetForm(); form.scrollIntoView({ behavior: "smooth" }); });
  const resetBtn = document.getElementById("template-reset-btn");
  if (resetBtn) resetBtn.addEventListener("click", resetForm);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const html = currentHtml();
    if (!html.trim()) { setStatus(statusId, "Template body is required.", true); return; }
    const payload = {
      name: (nameInput.value || "").trim(),
      subject: (subjectInput.value || "").trim(),
      html_content: html,
      plain_text_content: stripHtml(html),
    };
    if (!payload.name) { setStatus(statusId, "Template name is required.", true); return; }
    const btn = document.getElementById("template-save-btn");
    if (btn) btn.disabled = true;
    setStatus(statusId, "Saving...", false);
    try {
      const id = idInput.value;
      const result = id
        ? await postJSON("/api/templates/" + id, payload, "PATCH")
        : await postJSON("/api/templates", payload);
      const entry = result.template || {};
      window.location.href = "/templates";
      return entry;
    } catch (err) {
      setStatus(statusId, "Error: " + err.message, true);
      if (btn) btn.disabled = false;
    }
  });

  const previewBtn = document.getElementById("template-preview-btn");
  if (previewBtn) {
    previewBtn.addEventListener("click", async () => {
      const html = currentHtml();
      if (!html.trim()) { setStatus(statusId, "Nothing to preview.", true); return; }
      previewBtn.disabled = true;
      setStatus(statusId, "Rendering preview...", false);
      try {
        const result = await postJSON("/api/templates/preview", { html_content: html });
        if (previewFrame) previewFrame.srcdoc = result.html || "";
        if (previewWrap) previewWrap.hidden = false;
        setStatus(statusId, "Preview rendered.", false);
      } catch (err) {
        setStatus(statusId, "Error: " + err.message, true);
      } finally {
        previewBtn.disabled = false;
      }
    });
  }
}

// ------------------------------------------------------------------ //
// Global settings (defaults for all campaigns)
// ------------------------------------------------------------------ //
function initGlobalSettingsForm() {
  const form = document.getElementById("global-settings-form");
  if (!form) return;

  // Rate presets just fill the number input.
  form.querySelectorAll("[data-rate-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = form.querySelector("[name=send_rate_per_minute]");
      if (input) input.value = btn.dataset.ratePreset;
    });
  });

  // Plan hint: show recommended values + hard limits for the chosen plan.
  const planSelect = document.getElementById("brevo-plan-select");
  let plansCache = null;
  async function renderPlanHint() {
    if (!planSelect) return;
    const host = document.getElementById("plan-hint");
    if (!plansCache) {
      try {
        const res = await fetch("/api/settings/plans");
        plansCache = (await res.json()).plans || {};
      } catch (_) { plansCache = {}; }
    }
    const p = plansCache[planSelect.value];
    if (!host || !p) return;
    host.innerHTML =
      `<strong>${escapeHtml(p.label)}</strong> recommends ` +
      `<strong>${p.recommended.send_rate_per_minute}</strong>/min, ` +
      `<strong>${p.recommended.max_recipients_per_hour}</strong>/hour, ` +
      `<strong>${p.recommended.max_recipients_per_day}</strong>/day. ` +
      `Hard limits: ${p.limits.send_rate_per_minute}/min, ` +
      `${p.limits.max_recipients_per_hour}/hour, ${p.limits.max_recipients_per_day}/day. ` +
      `<em>${escapeHtml(p.note)}</em>`;
  }
  if (planSelect) {
    planSelect.addEventListener("change", renderPlanHint);
    renderPlanHint();
  }

  // Kill-switch toggle reflects its own state immediately.
  const pauseToggle = form.querySelector("[name=sending_paused]");
  if (pauseToggle) {
    pauseToggle.addEventListener("change", () => {
      setStatus("form-status",
        pauseToggle.checked ? "Sending will be paused after save." : "Sending will be enabled after save.",
        false);
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const fd = new FormData(form);
    const payload = {};

    ["sender_email", "sender_name", "sender_domain", "reply_to_email", "operator_email",
     "public_base_url", "default_window_start", "default_window_end", "default_window_timezone"]
      .forEach((k) => { const v = (fd.get(k) || "").trim(); if (v) payload[k] = v; });

    // Allow clearing reply-to/operator/public URL by sending an empty string.
    ["reply_to_email", "operator_email", "public_base_url"].forEach((k) => {
      if (fd.has(k) && !(fd.get(k) || "").trim()) payload[k] = null;
    });

    const domainsRaw = (fd.get("allowed_sender_domains") || "").trim();
    payload.allowed_sender_domains = domainsRaw
      ? domainsRaw.split(/[\s,;]+/).map((d) => d.trim()).filter(Boolean)
      : [];

    ["max_hard_bounce_rate", "max_complaint_rate", "default_batch_size",
     "send_rate_per_minute", "max_recipients_per_hour", "max_recipients_per_day",
     "default_stagger_seconds"]
      .forEach((k) => {
        const raw = (fd.get(k) || "").toString().trim();
        if (raw !== "") payload[k] = Number(raw);
      });

    if (pauseToggle) payload.sending_paused = pauseToggle.checked;
    if (planSelect) payload.brevo_plan = planSelect.value;

    ["telegram_bot_token", "telegram_chat_id"].forEach((k) => {
      const v = (fd.get(k) || "").trim();
      if (v) payload[k] = v;
    });

    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    setStatus("form-status", "Saving...", false);
    try {
      const result = await postJSON("/api/settings", payload, "PATCH");
      if (result.warnings && result.warnings.length) {
        setStatus("form-status", "Saved with warnings: " + result.warnings.join(" "), true);
      } else {
        setStatus("form-status", "Saved.", false);
      }
    } catch (err) {
      setStatus("form-status", "Error: " + err.message, true);
    } finally {
      button.disabled = false;
    }
  });

  const testEmailBtn = document.getElementById("test-email-btn");
  if (testEmailBtn) {
    testEmailBtn.addEventListener("click", async () => {
      const to = (document.getElementById("test-email-to") || {}).value || "";
      testEmailBtn.disabled = true;
      setStatus("form-status", "Sending test email...", false);
      try {
        const r = await postJSON("/api/settings/test-email", { to });
        setStatus("form-status", "Test email sent to " + r.to + ".", false);
      } catch (err) {
        setStatus("form-status", "Test email failed: " + err.message, true);
      } finally {
        testEmailBtn.disabled = false;
      }
    });
  }

  const testTgBtn = document.getElementById("test-telegram-btn");
  if (testTgBtn) {
    testTgBtn.addEventListener("click", async () => {
      testTgBtn.disabled = true;
      setStatus("form-status", "Sending Telegram test...", false);
      try {
        await postJSON("/api/settings/test-telegram", { section: "email" });
        setStatus("form-status", "Telegram test sent — check your chat.", false);
      } catch (err) {
        setStatus("form-status", "Telegram test failed: " + err.message, true);
      } finally {
        testTgBtn.disabled = false;
      }
    });
  }

  const clearTgBtn = document.getElementById("clear-telegram-btn");
  if (clearTgBtn) {
    clearTgBtn.addEventListener("click", async () => {
      clearTgBtn.disabled = true;
      setStatus("form-status", "Clearing Telegram overrides...", false);
      try {
        await postJSON("/api/settings", { telegram_bot_token: "", telegram_chat_id: "" }, "PATCH");
        form.querySelector("[name=telegram_bot_token]").value = "";
        form.querySelector("[name=telegram_chat_id]").value = "";
        setStatus("form-status", "Telegram overrides cleared; using global fallback.", false);
      } catch (err) {
        setStatus("form-status", "Clear failed: " + err.message, true);
      } finally {
        clearTgBtn.disabled = false;
      }
    });
  }
}

// ------------------------------------------------------------------ //
// Campaign send-test button
// ------------------------------------------------------------------ //
function initSendTest() {
  const form = document.getElementById("send-test-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const fd = new FormData(form);
    const to = (fd.get("to") || "").trim();
    const campaignId = form.dataset.campaignId;
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    setStatus("send-test-status", "Sending test...", false);
    try {
      const r = await postJSON("/api/campaigns/" + campaignId + "/send-test", { to });
      setStatus("send-test-status", "Test sent to " + r.to + " (subject prefixed [TEST]).", false);
    } catch (err) {
      setStatus("send-test-status", "Error: " + err.message, true);
    } finally {
      btn.disabled = false;
    }
  });
}

// ------------------------------------------------------------------ //
// Deliverability report page
// ------------------------------------------------------------------ //
function initDeliverability() {
  const host = document.getElementById("deliverability-report");
  if (!host) return;
  const domainInput = document.getElementById("deliverability-domain");
  const runBtn = document.getElementById("deliverability-run");
  const status = document.getElementById("deliverability-status");

  function badge(ok, text) {
    return `<span class="badge ${ok ? "badge-ok" : "badge-warn"}">${text}</span>`;
  }

  function render(report) {
    const dns = report.dns || {};
    const brevo = report.brevo || {};
    const dkim = dns.dkim || {};
    const dkimLines = Object.entries(dkim.selectors || {}).map(
      ([sel, info]) => `<li>${sel}: ${info.ok ? "OK" : "missing"}${info.target ? " → " + info.target : ""}</li>`
    ).join("");
    const recs = (report.recommendations || []).map(
      (r) => `<li class="rec-${r.level}">${r.message}</li>`
    ).join("") || "<li class=\"muted\">No issues found — looking good.</li>";

    host.innerHTML = `
      <div class="card-grid">
        <div class="card"><span class="card-label">Domain</span>
          <span class="card-value">${report.domain}</span></div>
        <div class="card"><span class="card-label">Brevo verified</span>
          <div>${badge(brevo.verified, brevo.verified ? "verified" : "not verified")}
          ${badge(brevo.authenticated, brevo.authenticated ? "authenticated" : "not authenticated")}</div></div>
        <div class="card"><span class="card-label">DKIM</span>
          <div>${badge(dkim.ok, dkim.ok ? "present" : "missing")}</div>
          <ul class="muted small">${dkimLines || "<li>no selectors checked</li>"}</ul></div>
        <div class="card"><span class="card-label">SPF</span>
          <div>${badge(dns.spf && dns.spf.ok, dns.spf && dns.spf.present ? "present" : "missing")}</div>
          <div class="muted small">${(dns.spf && dns.spf.value) || ""}</div></div>
        <div class="card"><span class="card-label">DMARC</span>
          <div>${badge(dns.dmarc && dns.dmarc.present, dns.dmarc && dns.dmarc.present ? "present" : "missing")}</div>
          <div class="muted small">${(dns.dmarc && dns.dmarc.value) || ""}</div></div>
        <div class="card"><span class="card-label">MX</span>
          <div>${badge(dns.mx && dns.mx.ok, dns.mx && dns.mx.ok ? "present" : "optional")}</div>
          <div class="muted small">${(dns.mx && dns.mx.records || []).join(", ") || "not required for outbound-only sending"}</div></div>
        <div class="card"><span class="card-label">Tracking URL</span>
          <div>${badge(report.public_base_url_ok, report.public_base_url_ok ? "public" : "localhost")}</div>
          <div class="muted small">${report.public_base_url || ""}</div></div>
      </div>
      <h3>Recommendations</h3>
      <ul class="rec-list">${recs}</ul>
      ${renderReputation(report.reputation)}`;
  }

  async function run() {
    const domain = domainInput ? domainInput.value.trim() : "";
    status.textContent = "Checking...";
    runBtn.disabled = true;
    try {
      const res = await fetch("/api/deliverability" + (domain ? "?domain=" + encodeURIComponent(domain) : ""));
      const data = await res.json();
      if (!res.ok) throw new Error(JSON.stringify(data.error || res.statusText));
      render(data);
      status.textContent = "Checked " + new Date().toLocaleTimeString();
    } catch (err) {
      status.textContent = "Error: " + err.message;
    } finally {
      runBtn.disabled = false;
    }
  }

  if (runBtn) runBtn.addEventListener("click", run);
  run();
}

// Reputation analytics block (rendered inside the deliverability report).
function renderReputation(rep) {
  if (!rep) return "";
  const rates = rep.rates || {};
  const totals = rep.totals || {};
  const rateCard = (label, value, warn) => {
    const cls = warn ? "badge-warn" : "badge-ok";
    return `<div class="card"><span class="card-label">${label}</span>
      <span class="card-value">${value}%</span></div>`;
  };
  // Gmail guidance: bounce < 2%, complaint < 0.1%.
  const bounceWarn = (rates.bounces || 0) >= 2;
  const complaintWarn = (rates.complaints || 0) >= 0.1;
  const domains = (rep.top_bounce_domains || []).map(
    (d) => `<li>${d.domain} <span class="muted">(${d.count})</span></li>`
  ).join("") || "<li class=\"muted\">No bubbled addresses yet.</li>";
  const suppression = rep.suppression || {};
  const suppressionLine = Object.entries(suppression)
    .map(([k, v]) => `<span class="badge badge-${k}">${k.replace("_", " ")}: ${v}</span>`)
    .join(" ") || "<span class=\"muted\">none</span>";

  return `
    <h3>Reputation analytics</h3>
    <div class="card-grid">
      <div class="card"><span class="card-label">Sent (all time)</span>
        <span class="card-value">${totals.sent || 0}</span></div>
      ${rateCard("Bounce rate", (rates.bounces || 0).toFixed(2), bounceWarn)}
      ${rateCard("Complaint rate", (rates.complaints || 0).toFixed(2), complaintWarn)}
      <div class="card"><span class="card-label">Open rate</span>
        <span class="card-value">${(rates.opens || 0).toFixed(2)}%</span></div>
      <div class="card"><span class="card-label">Click rate</span>
        <span class="card-value">${(rates.clicks || 0).toFixed(2)}%</span></div>
      <div class="card"><span class="card-label">Reusable contacts</span>
        <span class="card-value">${rep.deliverable_contacts || 0}</span></div>
    </div>
    <div class="grid-2">
      <div>
        <h4>Top bounce domains</h4>
        <ul class="muted small">${domains}</ul>
      </div>
      <div>
        <h4>Suppressed contacts</h4>
        <div>${suppressionLine}</div>
      </div>
    </div>`;
}


// ------------------------------------------------------------------ //
// Delete buttons (campaigns & contacts)
// ------------------------------------------------------------------ //
function initDeletes() {
  document.querySelectorAll("[data-delete-campaign]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.dataset.deleteCampaign;
      if (!window.confirm("Delete this campaign and all its recipient records?")) return;
      button.disabled = true;
      try {
        await postJSON("/api/campaigns/" + id, null, "DELETE");
        window.location.href = "/";
      } catch (err) {
        setStatus("action-status", "Error: " + err.message, true);
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-delete-contact]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.dataset.deleteContact;
      if (!window.confirm("Delete this contact?")) return;
      button.disabled = true;
      try {
        await postJSON("/api/contacts/" + id, null, "DELETE");
        const row = button.closest("tr");
        if (row) row.remove();
      } catch (err) {
        window.alert("Error: " + err.message);
        button.disabled = false;
      }
    });
  });
}

// ------------------------------------------------------------------ //
// Campaign action buttons (start / pause / resume / cancel)
// ------------------------------------------------------------------ //
function initCampaignActions() {
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.action;
      const campaign = button.dataset.campaign;

      let message = "Are you sure you want to " + action + " this campaign?";
      if (action === "start") {
        const count = Number(button.dataset.recipients || "0");
        if (count === 0) {
          window.alert("This campaign has no recipients. Add contacts or lists before starting.");
          return;
        }
        message = "Start sending to " + count + " recipient(s)?\n\n" +
          "Scheduling is global: overlapping campaigns are spaced automatically and a " +
          "high-priority campaign pauses the others. Continue?";
      }
      if (action === "restart") {
        message = "Restart this campaign from where it stopped?\n\n" +
          "Only queued recipients will be emailed. Contacts that already received this " +
          "campaign — including the address that hard-bounced — are not emailed again.";
      }
      if (!window.confirm(message)) return;

      button.disabled = true;
      setStatus("action-status", action + "ing...", false);
      try {
        await postJSON("/api/campaigns/" + campaign + "/action", { action });
        window.location.reload();
      } catch (err) {
        setStatus("action-status", "Error: " + err.message, true);
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-priority-toggle]").forEach((button) => {
    button.addEventListener("click", async () => {
      const campaign = button.dataset.priorityToggle;
      const makeHigh = button.dataset.current !== "high";
      if (makeHigh && !window.confirm(
        "Make this campaign high priority?\n\n" +
        "While it has recipients left, other active campaigns will wait (they are not " +
        "paused and resume automatically). Only one campaign can be high priority.")) return;
      button.disabled = true;
      try {
        await postJSON("/api/campaigns/" + campaign + "/priority", { high: makeHigh });
        window.location.reload();
      } catch (err) {
        window.alert("Error: " + err.message);
        button.disabled = false;
      }
    });
  });
}

// ------------------------------------------------------------------ //
// Contact lists page
// ------------------------------------------------------------------ //
function initLists() {
  const container = document.getElementById("lists-container");
  if (!container) return;

  const createForm = document.getElementById("list-create-form");
  if (createForm) {
    createForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const fd = new FormData(createForm);
      const emailsRaw = (fd.get("emails") || "").trim();
      const payload = {
        name: (fd.get("name") || "").trim(),
        description: (fd.get("description") || "").trim() || null,
        emails: emailsRaw
          ? emailsRaw.split(/[,\n;]+/).map((s) => s.trim()).filter(Boolean)
          : [],
      };
      if (!payload.name) { setStatus("list-status", "Name is required.", true); return; }
      const btn = createForm.querySelector("button[type=submit]");
      btn.disabled = true;
      setStatus("list-status", "Creating...", false);
      try {
        await postJSON("/api/lists", payload);
        window.location.reload();
      } catch (err) {
        setStatus("list-status", "Error: " + err.message, true);
        btn.disabled = false;
      }
    });
  }

  function renderMembers(listId, data) {
    const host = document.getElementById("members-" + listId);
    if (!host) return;
    const rows = (data.members || []).map((m) => `
      <tr>
        <td>${m.email}</td>
        <td>${[m.first_name, m.last_name].filter(Boolean).join(" ") || "—"}</td>
        <td><span class="badge badge-${m.status}">${m.status}</span></td>
        <td class="num">
          <button type="button" class="btn btn-sm btn-danger"
                  data-remove-member="${listId}" data-contact-id="${m.id}">Remove</button>
        </td>
      </tr>`).join("") || '<tr><td colspan="4" class="muted">No members yet.</td></tr>';

    host.innerHTML = `
      <table class="table">
        <thead><tr><th>Email</th><th>Name</th><th>Status</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <form class="inline-row add-member-form" data-list-id="${listId}">
        <input type="email" name="email" placeholder="add@email.com" required>
        <button type="submit" class="btn btn-sm">Add member</button>
        <span class="muted member-status"></span>
      </form>`;
  }

  container.addEventListener("click", async (event) => {
    const toggle = event.target.closest("[data-list-toggle]");
    if (toggle) {
      const listId = toggle.dataset.listToggle;
      const panel = document.getElementById("members-" + listId);
      if (!panel.hidden) { panel.hidden = true; toggle.textContent = "Manage"; return; }
      try {
        const res = await fetch("/api/lists/" + listId);
        const data = await res.json();
        if (!res.ok) throw new Error(JSON.stringify(data.error || res.statusText));
        renderMembers(listId, data);
        panel.hidden = false;
        toggle.textContent = "Hide";
      } catch (err) {
        window.alert("Error: " + err.message);
      }
      return;
    }

    const del = event.target.closest("[data-delete-list]");
    if (del) {
      const listId = del.dataset.deleteList;
      if (!window.confirm("Delete this list? Campaigns keep their already-added recipients.")) return;
      del.disabled = true;
      try {
        await postJSON("/api/lists/" + listId, null, "DELETE");
        const card = del.closest(".list-card");
        if (card) card.remove();
      } catch (err) {
        window.alert("Error: " + err.message);
        del.disabled = false;
      }
      return;
    }

    const remove = event.target.closest("[data-remove-member]");
    if (remove) {
      const listId = remove.dataset.removeMember;
      try {
        await postJSON("/api/lists/" + listId + "/members/" + remove.dataset.contactId,
          null, "DELETE");
        const row = remove.closest("tr");
        if (row) row.remove();
      } catch (err) {
        window.alert("Error: " + err.message);
      }
    }
  });

  container.addEventListener("submit", async (event) => {
    const form = event.target.closest(".add-member-form");
    if (!form) return;
    event.preventDefault();
    const listId = form.dataset.listId;
    const email = (new FormData(form).get("email") || "").trim();
    const btn = form.querySelector("button[type=submit]");
    const status = form.querySelector(".member-status");
    btn.disabled = true;
    setStatus(status, "Adding...", false);
    try {
      const r = await postJSON("/api/lists/" + listId + "/members", { emails: [email] });
      renderMembers(listId, r);
    } catch (err) {
      setStatus(status, "Error: " + err.message, true);
      btn.disabled = false;
    }
  });
}

// ------------------------------------------------------------------ //
// Dashboard "See all" expanders
// ------------------------------------------------------------------ //
function initSeeMore() {
  document.querySelectorAll("[data-see-more-toggle]").forEach((button) => {
    const labelShow = button.textContent;
    button.addEventListener("click", () => {
      const rows = document.querySelectorAll("[data-see-more]");
      const expanded = button.dataset.expanded === "1";
      rows.forEach((row) => { row.hidden = expanded; });
      button.dataset.expanded = expanded ? "0" : "1";
      button.textContent = expanded ? labelShow : "Show less";
    });
  });
}

// ------------------------------------------------------------------ //
// Auth: log out button in the top bar
// ------------------------------------------------------------------ //
function initAuth() {
  const button = document.getElementById("logout-btn");
  if (!button) return;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await postJSON("/api/auth/logout", {});
    } catch (_) { /* ignore — just leave */ }
    window.location.href = "/login";
  });
}

// ------------------------------------------------------------------ //
// List picker (checkbox dropdown in the campaign composer)
// ------------------------------------------------------------------ //
function initListPicker() {
  document.querySelectorAll("[data-list-picker]").forEach((picker) => {
    const toggle = picker.querySelector(".list-picker-toggle");
    const menu = picker.querySelector(".list-picker-menu");
    const summary = picker.querySelector(".list-picker-summary");
    if (!toggle || !menu) return;

    const items = () => Array.from(picker.querySelectorAll(".list-picker-item"));

    function refreshSummary() {
      const selected = items().filter((el) => el.checked);
      if (!selected.length) {
        summary.textContent = "Select lists…";
        summary.classList.add("muted");
      } else {
        summary.textContent = selected.map((el) => el.dataset.name).join(", ");
        summary.classList.remove("muted");
      }
    }

    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      picker.classList.toggle("open", open);
    });

    menu.addEventListener("change", refreshSummary);

    document.addEventListener("click", (event) => {
      if (picker.contains(event.target)) return;
      menu.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      picker.classList.remove("open");
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        menu.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
        picker.classList.remove("open");
      }
    });

    refreshSummary();
  });
}

// ------------------------------------------------------------------ //
// Campaign reuse (clone / resend)
// ------------------------------------------------------------------ //
function initReuse() {
  const buttons = document.querySelectorAll("[data-reuse]");
  if (!buttons.length) return;
  const modeEl = document.getElementById("reuse-mode");
  buttons.forEach((button) => {
    button.addEventListener("click", async () => {
      const campaign = button.dataset.campaign;
      const kind = button.dataset.reuse;
      const mode = modeEl ? modeEl.value : "new";
      if (kind === "resend" && !window.confirm(
        "Resend this campaign to the selected recipients now?")) return;
      button.disabled = true;
      setStatus("reuse-status", kind === "clone" ? "Cloning…" : "Resending…", false);
      try {
        const result = await postJSON(
          "/api/campaigns/" + campaign + "/" + kind, { mode });
        if (kind === "clone") {
          window.location.href = "/campaigns/" + result.campaign.id + "/edit";
        } else {
          window.location.reload();
        }
      } catch (err) {
        setStatus("reuse-status", "Error: " + err.message, true);
        button.disabled = false;
      }
    });
  });
}

// ------------------------------------------------------------------ //
// Contacts: repair duplicated first/last names
// ------------------------------------------------------------------ //
function initCleanupNames() {
  const button = document.getElementById("cleanup-names-btn");
  if (!button) return;
  button.addEventListener("click", async () => {
    if (!window.confirm(
      "Clear every last name that is identical to its first name?\n\n" +
      "Use this to repair imports where the same value landed in both fields.")) return;
    button.disabled = true;
    try {
      const result = await postJSON("/api/contacts/cleanup-names", {});
      window.alert("Cleaned " + result.cleaned + " contact(s).");
      window.location.reload();
    } catch (err) {
      window.alert("Error: " + err.message);
      button.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  // Each initialiser is isolated: a missing element on one page must not stop
  // the buttons wired by the initialisers after it.
  [
    initInfoModals, initCampaignEditor, initTemplates, initSettingsForm,
    initGlobalSettingsForm, initSendTest, initDeliverability, initLists,
    initImport, initContactsHub, initCampaignActions, initSeeMore, initAuth,
    initListPicker, initReuse, initCleanupNames, initDeletes,
  ].forEach((init) => {
    try {
      init();
    } catch (err) {
      console.error("init failed:", init.name, err);
    }
  });
});
