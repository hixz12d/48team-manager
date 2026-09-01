(() => {
  const controllers = new Map();
  const palette = document.getElementById("command-palette");
  const commandInput = document.getElementById("command-input");
  const commandList = document.getElementById("command-list");
  const drawer = document.getElementById("operations-drawer");

  const destinations = [
    { label: "Go to Overview", href: "/" },
    { label: "Go to Workspaces", href: "/workspaces" },
    { label: "Go to Accounts", href: "/accounts" },
    { label: "Go to Operations", href: "/operations" },
    { label: "Open Settings", href: "/settings" },
  ];

  function abortEntity(key) {
    const previous = controllers.get(key);
    if (previous) previous.abort();
    const next = new AbortController();
    controllers.set(key, next);
    return next;
  }

  async function fetchEntity(key, url) {
    const controller = abortEntity(key);
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`query failed: ${key}`);
    return response.json();
  }

  function renderOverview(payload) {
    const root = document.getElementById("overview-attention");
    if (!root) return;
    if (!payload.attention || payload.attention.length === 0) {
      root.textContent = "All systems healthy";
      return;
    }
    root.replaceChildren();
    payload.attention.forEach((item) => {
      const row = document.createElement("div");
      row.textContent = item.message || item;
      root.append(row);
    });
  }

  function cell(row, text) {
    const td = document.createElement("td");
    td.textContent = text == null || text === "" ? "—" : String(text);
    row.append(td);
  }

  function renderRows(bodyId, items, emptyText, columns, renderItem) {
    const body = document.getElementById(bodyId);
    if (!body) return;
    body.replaceChildren();
    if (!items.length) {
      const row = document.createElement("tr");
      const empty = document.createElement("td");
      empty.colSpan = columns;
      empty.className = "muted";
      empty.textContent = emptyText;
      row.append(empty);
      body.append(row);
      return;
    }
    items.forEach((item) => body.append(renderItem(item)));
  }

  function workspaceRow(item) {
    const row = document.createElement("tr");
    row.dataset.entityId = String(item.id);
    cell(row, item.name);
    cell(row, item.owner_email);
    cell(row, item.members);
    cell(row, item.quota);
    cell(row, item.rotation);
    cell(row, item.last_sync);
    cell(row, item.status);
    return row;
  }

  function accountRow(item) {
    const row = document.createElement("tr");
    row.dataset.entityId = String(item.id);
    cell(row, item.email);
    cell(row, item.purpose);
    cell(row, item.workspace);
    cell(row, item.quota_7d);
    cell(row, item.auth);
    cell(row, item.sub2api);
    cell(row, item.proxy);
    cell(row, item.state);
    return row;
  }

  function operationRow(item) {
    const row = document.createElement("tr");
    row.dataset.entityId = String(item.id);
    cell(row, item.status);
    cell(row, item.operation);
    cell(row, item.target);
    cell(row, item.current_step);
    cell(row, item.started);
    cell(row, item.duration);
    return row;
  }

  function phoneRow(item) {
    const row = document.createElement("tr");
    row.dataset.entityId = String(item.id);
    cell(row, item.number);
    cell(row, item.status);
    cell(row, item.used_count);
    cell(row, item.remaining);
    cell(row, item.reserved_by);
    cell(row, item.last_error_type);
    return row;
  }

  function hmeRow(item) {
    const row = document.createElement("tr");
    row.dataset.entityId = String(item.id);
    cell(row, item.email);
    cell(row, item.state);
    cell(row, item.label);
    cell(row, item.pending ? "pending" : "");
    cell(row, item.job_id);
    return row;
  }

  function proxyRow(item) {
    const row = document.createElement("tr");
    row.dataset.entityId = String(item.id);
    cell(row, item.name);
    cell(row, `${item.scheme}://${item.host}:${item.port}`);
    cell(row, item.status);
    cell(row, item.region);
    cell(row, item.last_exit_ip);
    return row;
  }

  async function bootPage() {
    const page = document.body.dataset.page;
    try {
      if (page === "overview") {
        renderOverview(await fetchEntity("overview", "/api/overview"));
      } else if (page === "workspaces") {
        const payload = await fetchEntity("workspace-list", "/api/workspaces");
        renderRows("workspaces-body", payload.items || [], "No workspaces yet.", 7, workspaceRow);
      } else if (page === "accounts") {
        const filter = document.querySelector("[data-filter='purpose']");
        if (filter && !filter.dataset.bound) {
          filter.dataset.bound = "1";
          filter.addEventListener("change", () => bootPage());
        }
        const purpose = filter?.value || "all";
        const includeArchived = purpose === "archived";
        const payload = await fetchEntity(
          "account-list",
          `/api/accounts?purpose=${encodeURIComponent(purpose)}&include_archived=${includeArchived}`
        );
        renderRows("accounts-body", payload.items || [], "No accounts yet. Archived rows stay hidden by default.", 8, accountRow);
      } else if (page === "operations") {
        const payload = await fetchEntity("operation-list", "/api/operations");
        renderRows("operations-body", payload.items || [], "No operations.", 6, operationRow);
      } else if (page === "phones") {
        const payload = await fetchEntity("phone-list", "/api/resources/phones");
        renderRows("phones-body", payload.items || [], "No phones yet.", 6, phoneRow);
      } else if (page === "hme") {
        const payload = await fetchEntity("hme-list", "/api/resources/hme");
        renderRows("hme-body", payload.items || [], "No HME leases.", 5, hmeRow);
      } else if (page === "proxies") {
        const payload = await fetchEntity("proxy-list", "/api/resources/proxies");
        renderRows("proxies-body", payload.items || [], "No proxy profiles.", 5, proxyRow);
      }
    } catch (error) {
      if (error.name !== "AbortError") console.warn(error);
    }
  }

  function openDrawer() {
    drawer.hidden = false;
  }

  function closeDrawer() {
    drawer.hidden = true;
  }

  function renderPalette(query) {
    const q = query.trim().toLowerCase();
    commandList.replaceChildren();
    destinations
      .filter((item) => item.label.toLowerCase().includes(q))
      .forEach((item) => {
        const li = document.createElement("li");
        li.textContent = item.label;
        li.addEventListener("click", () => {
          window.location.href = item.href;
        });
        commandList.append(li);
      });
  }

  document.getElementById("open-operations")?.addEventListener("click", openDrawer);
  document.querySelector("[data-close-drawer]")?.addEventListener("click", closeDrawer);
  drawer?.addEventListener("click", (event) => {
    if (event.target === drawer) closeDrawer();
  });

  window.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      palette.showModal();
      renderPalette("");
      commandInput.focus();
    }
    if (event.key === "Escape") {
      closeDrawer();
    }
  });
  commandInput?.addEventListener("input", () => renderPalette(commandInput.value));

  window.Team48 = { abortEntity, fetchEntity };
  bootPage();
})();
