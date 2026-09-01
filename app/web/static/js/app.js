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

  function renderTable(bodyId, items, emptyText, columns) {
    const body = document.getElementById(bodyId);
    if (!body) return;
    body.replaceChildren();
    if (!items.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = columns;
      cell.className = "muted";
      cell.textContent = emptyText;
      row.append(cell);
      body.append(row);
      return;
    }
    items.forEach((item) => {
      const row = document.createElement("tr");
      row.dataset.entityId = String(item.id);
      row.addEventListener("click", () => {
        /* drawer per entity lands with identity queries */
      });
      Object.keys(item).slice(0, columns).forEach((key) => {
        const cell = document.createElement("td");
        cell.textContent = item[key] ?? "";
        row.append(cell);
      });
      body.append(row);
    });
  }

  async function bootPage() {
    const page = document.body.dataset.page;
    try {
      if (page === "overview") {
        renderOverview(await fetchEntity("overview", "/api/overview"));
      } else if (page === "workspaces") {
        const payload = await fetchEntity("workspace-list", "/api/workspaces");
        renderTable("workspaces-body", payload.items || [], "No workspaces yet. Identity lands in the next phase.", 7);
      } else if (page === "accounts") {
        const payload = await fetchEntity("account-list", "/api/accounts");
        renderTable("accounts-body", payload.items || [], "No accounts yet. Archived rows stay hidden by default.", 8);
      } else if (page === "operations") {
        const payload = await fetchEntity("operation-list", "/api/operations");
        renderTable("operations-body", payload.items || [], "No operations.", 6);
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
