(() => {
  const isAppView = document.querySelector("#search-input") || document.querySelector(".dashboard-page");
  if (isAppView) {
    const url = new URL(window.location.href);
    if (url.searchParams.has("clientId")) {
      url.searchParams.delete("clientId");
      window.history.replaceState({}, "", url);
    }
  }

  const errorBanner = document.querySelector(".app-error");
  function showError(message) {
    errorBanner.textContent = message;
    errorBanner.hidden = !message;
  }

  async function api(path, options = {}) {
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
    const response = await fetch(path, {
      ...options,
      headers: { "content-type": "application/json", ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}), ...options.headers },
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `Request failed (${response.status}).`);
    return body;
  }

  function navigate(view) {
    const destination = new URL(window.location.href);
    if (view === "customer") destination.searchParams.delete("view");
    else destination.searchParams.set("view", view);
    destination.searchParams.delete("clientId");
    window.location.assign(destination);
  }

  document.querySelectorAll("button[data-view]").forEach((button) => {
    button.addEventListener("click", () => navigate(button.dataset.view));
  });

  const searchInput = document.querySelector("#search-input");
  if (searchInput) {
    const locationInput = document.querySelector("#location-input");
    const locationLabel = document.querySelector("#location-label");
    const chips = [...document.querySelectorAll(".category-chip")];
    const cards = [...document.querySelectorAll(".provider-card")];
    const emptyState = document.querySelector("#empty-state");
    const count = document.querySelector("#result-count");
    let activeCategory = "All services";
    let favoritesOnly = false;
    let targetProviderId = new URL(window.location.href).searchParams.get("providerId");

    function filterProviders() {
      const query = searchInput.value.trim().toLowerCase();
      const locationQuery = locationInput.value.trim().split(",")[0].toLowerCase();
      let visible = 0;
      for (const card of cards) {
        const searchable = card.textContent.toLowerCase();
        const matches = (!targetProviderId || card.dataset.providerId === targetProviderId)
          && (favoritesOnly ? card.dataset.favorite === "true" : activeCategory === "All services" || card.dataset.category === activeCategory)
          && (!query || searchable.includes(query))
          && (!locationQuery || searchable.includes(locationQuery));
        card.hidden = !matches;
        if (matches) visible += 1;
      }
      count.textContent = visible;
      if (emptyState) emptyState.hidden = visible !== 0;
      locationLabel.textContent = locationInput.value || "all locations";
    }

    searchInput.addEventListener("input", filterProviders);
    locationInput.addEventListener("input", filterProviders);
    document.querySelector("#search-submit").addEventListener("click", () => {
      document.querySelector(".results-heading").scrollIntoView({ behavior: "smooth" });
      filterProviders();
    });
    document.querySelector("#all-nearby").addEventListener("click", () => {
      activeCategory = "All services";
      favoritesOnly = false;
      targetProviderId = null;
      chips.forEach((chip) => chip.classList.toggle("selected", chip.dataset.category === activeCategory));
      chips.forEach((chip) => chip.setAttribute("aria-pressed", String(chip.dataset.category === activeCategory)));
      filterProviders();
    });
    chips.forEach((chip) => chip.addEventListener("click", () => {
      activeCategory = chip.dataset.category;
      favoritesOnly = activeCategory === "Favorites";
      targetProviderId = null;
      chips.forEach((item) => item.classList.toggle("selected", item === chip));
      chips.forEach((item) => item.setAttribute("aria-pressed", String(item === chip)));
      filterProviders();
    }));
    document.querySelectorAll(".provider-card").forEach((card) => {
      card.dataset.favorite = card.dataset.favorite || "false";
    });
    document.querySelector("#clear-filters")?.addEventListener("click", () => {
      searchInput.value = "";
      activeCategory = "All services";
      favoritesOnly = false;
      targetProviderId = null;
      chips.forEach((chip) => chip.classList.toggle("selected", chip.dataset.category === activeCategory));
      chips.forEach((chip) => chip.setAttribute("aria-pressed", String(chip.dataset.category === activeCategory)));
      filterProviders();
    });
    filterProviders();

    async function joinQueue(button, services) {
      try {
        await api("/api/queues", {
          method: "POST",
          body: JSON.stringify({ providerId: button.dataset.providerId, ...(services ? { services } : {}) }),
        });
        window.location.reload();
      } catch (error) {
        showError(error.message);
      }
    }

    function openServicePicker(button, card, services) {
      document.querySelectorAll(".join-services-panel").forEach((panel) => panel.remove());
      document.querySelectorAll(".join-queue").forEach((item) => { item.hidden = false; });
      const panel = document.createElement("div");
      panel.className = "join-services-panel";
      const heading = document.createElement("p");
      heading.className = "join-services-heading";
      heading.textContent = "What service do you need?";
      panel.append(heading);
      const list = document.createElement("div");
      list.className = "join-services-list";
      const boxes = [];
      for (const service of services) {
        const label = document.createElement("label");
        label.className = "service-option";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = service.name;
        boxes.push(box);
        const text = document.createElement("span");
        text.textContent = service.name;
        label.append(box, text);
        if (service.minutes) {
          const minutes = document.createElement("span");
          minutes.className = "service-minutes";
          minutes.textContent = `${service.minutes} min`;
          label.append(minutes);
        }
        list.append(label);
      }
      panel.append(list);
      const note = document.createElement("p");
      note.className = "join-services-note";
      note.textContent = `You can pick more than one. Nothing picked? We’ll book “${services[0].name}”.`;
      panel.append(note);
      const actions = document.createElement("div");
      actions.className = "join-services-actions";
      const confirm = document.createElement("button");
      confirm.type = "button";
      confirm.className = "button button-dark";
      confirm.textContent = "Join queue";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "button button-outline";
      cancel.textContent = "Cancel";
      actions.append(confirm, cancel);
      panel.append(actions);
      confirm.addEventListener("click", async () => {
        confirm.disabled = true;
        await joinQueue(button, boxes.filter((box) => box.checked).map((box) => box.value));
      });
      cancel.addEventListener("click", () => {
        panel.remove();
        button.hidden = false;
      });
      button.hidden = true;
      card.querySelector(".card-footer").after(panel);
    }

    document.querySelectorAll(".join-queue").forEach((button) => button.addEventListener("click", () => {
      const card = button.closest(".provider-card");
      const services = card
        ? [...card.querySelectorAll(".service-tags span")]
            .map((tag) => ({ name: tag.textContent.trim(), minutes: Number(tag.dataset.minutes) || null }))
            .filter((service) => service.name)
        : [];
      if (button.classList.contains("button-joined") || services.length === 0) {
        // Already in line, or the provider lists no services: join directly —
        // the server books the default service.
        joinQueue(button);
        return;
      }
      openServicePicker(button, card, services);
    }));

    document.querySelectorAll(".leave-queue").forEach((button) => button.addEventListener("click", async () => {
      try {
        await api(`/api/queues/${encodeURIComponent(button.dataset.queueId)}`, {
          method: "DELETE",
        });
        window.location.reload();
      } catch (error) {
        showError(error.message);
      }
    }));

    document.querySelectorAll(".favorite-button").forEach((button) => button.addEventListener("click", async () => {
      const adding = button.getAttribute("aria-pressed") !== "true";
      button.disabled = true;
      try {
        await api(`/api/favorites/${encodeURIComponent(button.dataset.providerId)}`, {
          method: adding ? "POST" : "DELETE",
        });
        button.setAttribute("aria-pressed", String(adding));
        button.classList.toggle("is-favorite", adding);
        button.setAttribute("aria-label", `${adding ? "Remove" : "Add"} ${button.dataset.providerName} ${adding ? "from" : "to"} favorites`);
        button.closest(".provider-card").dataset.favorite = String(adding);
        if (favoritesOnly) filterProviders();
      } catch (error) {
        showError(error.message);
      } finally {
        button.disabled = false;
      }
    }));

    const myQueueRefresh = document.querySelector("#refresh-my-queue");
  if (myQueueRefresh) {
    myQueueRefresh.addEventListener("click", () => window.location.reload());
  }

  const queuePanels = [...document.querySelectorAll("[data-queue-panel]")];
    if (queuePanels.length) {
      const refreshQueues = async () => {
        try {
          const current = await api("/api/queues/current");
          const queues = current.queues || [];
          if (queues.length !== queuePanels.length) {
            window.location.reload();
            return;
          }
          for (const panel of queuePanels) {
            const entry = queues.find((item) => item.id === panel.dataset.queueId);
            if (!entry || entry.status !== panel.dataset.queueStatus
              || String(Boolean(entry.buffer)) !== panel.dataset.queueBuffer) {
              window.location.reload();
              return;
            }
            panel.querySelector(".queue-position").textContent = entry.position;
            panel.querySelector(".queue-wait").textContent = entry.wait;
            const predicted = panel.querySelector(".queue-predicted");
            if (predicted && entry.predictedTurnAt) {
              predicted.textContent = new Date(entry.predictedTurnAt)
                .toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
                .replace(/^0/, "");
            }
          }
        } catch (error) {
          showError(error.message);
        }
      };
      window.setInterval(refreshQueues, 15000);
    }
  }

  const providerPage = document.querySelector(".dashboard-page[data-provider-id]");
  if (providerPage) {
    const providerId = providerPage.dataset.providerId;
    const queueRows = document.querySelector("#queue-rows");
    const queueCount = document.querySelector("#people-count");
    const callNextButton = document.querySelector("#call-next");
    const noWaiting = document.querySelector("#no-waiting");
    const queueFilters = document.querySelectorAll("[data-queue-filter]");
    const peopleMetric = document.querySelector("#provider-people-metric");
    const waitMetric = document.querySelector("#provider-wait-metric");
    const serviceDuration = Number(providerPage.dataset.serviceDuration) || 15;
    const staffCount = Number(providerPage.dataset.staffCount) || 1;
    const presentationButton = document.querySelector("#toggle-presentation");
    const queueFilterHeading = document.querySelector(".heading-wait");
    let activeQueueFilter = "active";
    let activeServiceFilter = "all";
    presentationButton.addEventListener("click", () => {
      const expanded = presentationButton.getAttribute("aria-expanded") !== "true";
      providerPage.classList.toggle("presentation-expanded", expanded);
      providerPage.closest(".app-shell").classList.toggle("presentation-active", expanded);
      presentationButton.setAttribute("aria-expanded", String(expanded));
      presentationButton.textContent = expanded ? "Collapse display" : "Expand display";
    });

    function appendText(parent, tag, className, text) {
      const element = document.createElement(tag);
      if (className) element.className = className;
      element.textContent = text;
      parent.append(element);
      return element;
    }

    const REMOVE_REASON_LABELS = {
      no_show: "No-show",
      removed: "Removed",
      day_end: "Day ended",
    };

    function renderQueue(queues, filter = activeQueueFilter) {
      queueRows.replaceChildren();
      const showingCompleted = filter === "completed";
      const showingDequeued = filter === "dequeued";
      const servingCount = queues.filter((entry) => entry.status === "serving").length;
      const slotsFull = servingCount >= staffCount;
      let visibleCount = 0;
      for (const [index, entry] of queues.entries()) {
        if (activeServiceFilter !== "all" && !(entry.customerService || "").split(", ").includes(activeServiceFilter)) continue;
        visibleCount += 1;
        const row = document.createElement("div");
        row.className = "queue-table-row";
        row.dataset.queueId = entry.id;
        const customer = document.createElement("div");
        customer.className = "customer-cell";
        appendText(customer, "span", `customer-avatar avatar-${index % 5 + 1}`, (entry.customerName || "Customer").slice(0, 2).toUpperCase());
        const customerText = document.createElement("span");
        const nameElement = appendText(customerText, "strong", "", entry.customerName || "Customer");
        nameElement.setAttribute("data-i18n-name", "");
        const details = [entry.customerPhone, entry.customerEmail, entry.customerAddress].filter(Boolean).join(" · ")
          || (entry.status === "serving" ? "Service in progress" : "Customer");
        appendText(customerText, "small", "customer-contact", details);
        customer.append(customerText);
        row.append(customer);
        const serviceCell = document.createElement("span");
        serviceCell.className = "service-cell";
        const serviceNames = (entry.customerService || "").split(", ").filter(Boolean);
        if (serviceNames.length) {
          for (const name of serviceNames) appendText(serviceCell, "span", "service-pill", name);
        } else {
          serviceCell.textContent = "—";
        }
        row.append(serviceCell);
        appendText(row, "span", "wait-cell", showingCompleted
          ? formatCompletedAt(entry.completedAt)
          : showingDequeued
            ? formatCompletedAt(entry.removedAt)
            : entry.status === "serving" ? "Now" : `~${entry.estimatedWait} min`);
        appendText(row, "span", "predicted-cell", !showingCompleted && !showingDequeued && entry.status === "waiting" && entry.predictedTurnAt
          ? new Date(entry.predictedTurnAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
          : "—");
        const action = document.createElement("span");
        action.className = "queue-row-action";
        if (showingCompleted) {
          appendText(action, "span", "table-status completed", "Completed");
        } else if (showingDequeued) {
          appendText(action, "span", "table-status removed", REMOVE_REASON_LABELS[entry.removeReason] || "No-show");
          const button = appendText(action, "button", "button button-outline queue-action", "Restore");
          button.type = "button";
          button.dataset.action = "restore";
          button.dataset.queueId = entry.id;
        } else if (entry.status === "serving") {
          appendText(action, "span", "table-status next-up", "Serving");
          const button = appendText(action, "button", "button button-complete queue-action", "Complete");
          button.type = "button";
          button.dataset.action = "complete";
          button.dataset.queueId = entry.id;
        } else {
          if (entry.buffer) {
            appendText(action, "span", "table-status buffer", "Buffer waitlist");
          } else if (entry.carriedOver) {
            appendText(action, "span", "table-status carried", "Carried over");
          } else {
            appendText(action, "span", "table-status waiting", index === 0 ? "Next up" : "Waiting");
          }
          const button = appendText(action, "button", "button button-outline queue-action", "Start service");
          button.type = "button";
          button.dataset.action = "serve";
          button.dataset.queueId = entry.id;
          button.disabled = slotsFull;
          const removeButton = appendText(action, "button", "button button-outline queue-action queue-remove", "Remove");
          removeButton.type = "button";
          removeButton.dataset.action = "remove";
          removeButton.dataset.queueId = entry.id;
        }
        row.append(action);
        queueRows.append(row);
      }
      noWaiting.textContent = queues.length !== 0 && visibleCount === 0
        ? "No customers for this service."
        : showingCompleted ? "No completed services yet." : showingDequeued ? "No dequeued customers." : "No one waiting. Enjoy the breather.";
      noWaiting.hidden = visibleCount !== 0;
      queueCount.textContent = queues.length;
      const countLabel = queueCount.parentElement;
      countLabel.replaceChildren(queueCount);
      appendText(countLabel, "span", "", showingCompleted
        ? ` completed service${queues.length === 1 ? "" : "s"}`
        : showingDequeued
          ? ` dequeued customer${queues.length === 1 ? "" : "s"}`
          : ` customer${queues.length === 1 ? "" : "s"} in your queue`);
      queueFilterHeading.textContent = showingCompleted ? "COMPLETED" : showingDequeued ? "REMOVED" : "WAIT";
      callNextButton.hidden = showingCompleted || showingDequeued;
      if (!showingCompleted && !showingDequeued) {
        peopleMetric.textContent = queues.length;
        waitMetric.textContent = `${Math.ceil(queues.reduce((total, queue) => total + (queue.duration || serviceDuration), 0) / staffCount)} min`;
        callNextButton.disabled = queues.length === 0 || slotsFull;
      }
    }

    function formatCompletedAt(value) {
      if (!value) return "—";
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
    }

    function selectQueueFilter(filter) {
      activeQueueFilter = filter;
      queueFilters.forEach((button) => {
        const selected = button.dataset.queueFilter === filter;
        button.classList.toggle("is-selected", selected);
        button.setAttribute("aria-pressed", String(selected));
      });
    }

    async function refreshProviderQueue() {
      const filter = activeQueueFilter;
      try {
        const response = await api(`/api/providers/${encodeURIComponent(providerId)}/queues?status=${filter}`);
        if (filter === activeQueueFilter) renderQueue(response.queues, filter);
      } catch (error) {
        showError(error.message);
      }
    }

    queueFilters.forEach((button) => {
      button.addEventListener("click", async () => {
        selectQueueFilter(button.dataset.queueFilter);
        await refreshProviderQueue();
      });
    });

    const liveQueueCard = document.querySelector(".live-queue-card");
    const serviceFilterSelect = document.querySelector("#queue-service-filter");
    const viewStorageKey = "smartqueue.providerView";
    let savedView = {};
    try {
      savedView = JSON.parse(window.localStorage.getItem(viewStorageKey) || "{}");
    } catch (error) {
      savedView = {};
    }
    function persistView() {
      const columns = {};
      document.querySelectorAll("[data-view-toggle]").forEach((toggle) => {
        columns[toggle.dataset.viewToggle] = toggle.checked;
      });
      try {
        window.localStorage.setItem(viewStorageKey, JSON.stringify({ columns, serviceFilter: activeServiceFilter }));
      } catch (error) {
        /* storage unavailable; view options still apply to this page */
      }
    }
    function applyViewToggles() {
      document.querySelectorAll("[data-view-toggle]").forEach((toggle) => {
        liveQueueCard.classList.toggle(`hide-${toggle.dataset.viewToggle}`, !toggle.checked);
      });
      persistView();
    }
    document.querySelectorAll("[data-view-toggle]").forEach((toggle) => {
      if (savedView.columns && toggle.dataset.viewToggle in savedView.columns) {
        toggle.checked = Boolean(savedView.columns[toggle.dataset.viewToggle]);
      }
      toggle.addEventListener("change", applyViewToggles);
    });
    if (serviceFilterSelect) {
      if (savedView.serviceFilter && [...serviceFilterSelect.options].some((option) => option.value === savedView.serviceFilter)) {
        activeServiceFilter = savedView.serviceFilter;
        serviceFilterSelect.value = activeServiceFilter;
      }
      serviceFilterSelect.addEventListener("change", async () => {
        activeServiceFilter = serviceFilterSelect.value;
        persistView();
        await refreshProviderQueue();
      });
    }
    applyViewToggles();
    if (activeServiceFilter !== "all") refreshProviderQueue();

    document.querySelector("#call-next").addEventListener("click", async () => {
      try {
        await api(`/api/providers/${encodeURIComponent(providerId)}/call-next`, { method: "POST" });
        window.location.reload();
      } catch (error) {
        showError(error.message);
      }
    });
    queueRows.addEventListener("click", async (event) => {
      const button = event.target.closest(".queue-action");
      if (!button) return;
      if (button.dataset.action === "remove") {
        const name = button.closest(".queue-table-row")?.querySelector(".customer-cell strong")?.textContent || "This customer";
        if (!window.confirm(`Remove ${name} from the queue? They will appear in the Dequeued tab and can be restored if they show up.`)) return;
      }
      button.disabled = true;
      try {
        await api(`/api/providers/${encodeURIComponent(providerId)}/queues/${encodeURIComponent(button.dataset.queueId)}`, {
          method: "PATCH",
          body: JSON.stringify({ action: button.dataset.action }),
        });
        await refreshProviderQueue();
      } catch (error) {
        button.disabled = false;
        showError(error.message);
      }
    });
    document.querySelector("#walk-in-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const values = new FormData(form);
      const submit = form.querySelector('button[type="submit"]');
      const notice = document.querySelector("#walk-in-notice");
      const showNotice = (message, isError = false) => {
        if (!notice) return;
        notice.textContent = message;
        notice.classList.toggle("error", isError);
        notice.hidden = false;
      };
      submit.disabled = true;
      if (notice) notice.hidden = true;
      try {
        const result = await api(`/api/providers/${encodeURIComponent(providerId)}/walk-ins`, {
          method: "POST",
          body: JSON.stringify({
            name: values.get("name"),
            email: values.get("email"),
            phone: values.get("phone"),
            address: values.get("address"),
            services: values.getAll("services"),
          }),
        });
        form.reset();
        selectQueueFilter("active");
        await refreshProviderQueue();
        showNotice(result.queue?.buffer ? "Added to the buffer waitlist — today's queue is full." : "Walk-in added to the queue.");
      } catch (error) {
        showNotice(error.message, true);
      } finally {
        submit.disabled = false;
      }
    });
    document.querySelector("#toggle-open").addEventListener("click", async () => {
      const currentStatus = document.querySelector("#provider-status").textContent;
      try {
        await api(`/api/providers/${encodeURIComponent(providerId)}`, {
          method: "PATCH",
          body: JSON.stringify({ status: currentStatus === "Open" ? "Paused" : "Open" }),
        });
        window.location.reload();
      } catch (error) {
        showError(error.message);
      }
    });
    document.querySelector("#provider-photo-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const submit = form.querySelector('button[type="submit"]');
      submit.disabled = true;
      try {
        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
        const response = await fetch(`/api/providers/${encodeURIComponent(providerId)}/photo`, {
          method: "POST",
          headers: csrfToken ? { "X-CSRF-Token": csrfToken } : {},
          body: new FormData(form),
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || `Request failed (${response.status}).`);
        window.location.reload();
      } catch (error) {
        showError(error.message);
      } finally {
        submit.disabled = false;
      }
    });
    document.querySelector("#provider-settings")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const values = new FormData(form);
      const serviceDurations = {};
      form.querySelectorAll(".service-duration-input").forEach((input) => {
        const minutes = Number(input.value);
        if (input.value.trim() !== "" && Number.isInteger(minutes) && minutes >= 1 && minutes <= 180) {
          serviceDurations[input.dataset.service] = minutes;
        }
      });
      try {
        await api(`/api/providers/${encodeURIComponent(providerId)}`, {
          method: "PATCH",
          body: JSON.stringify({
            name: values.get("name"),
            category: values.get("category"),
            address: values.get("address"),
            description: values.get("description"),
            contactEmail: values.get("contactEmail"),
            contactPhone: values.get("contactPhone"),
            services: values.get("services").split(",").map((service) => service.trim()).filter(Boolean),
            hours: values.get("hours"),
            serviceDuration: Number(values.get("serviceDuration")),
            staffCount: Number(values.get("staffCount")),
            serviceDurations,
            openTime: values.get("openTime") || "",
            closeTime: values.get("closeTime") || "",
            timezone: values.get("timezone"),
            bufferMinutes: Number(values.get("bufferMinutes")),
            carryForwardQueue: form.querySelector('[name="carryForwardQueue"]')?.checked === true,
            privateListing: form.querySelector('[name="privateListing"]')?.checked === true,
          }),
        });
        window.location.reload();
      } catch (error) {
        showError(error.message);
      }
    });
    let refreshReport = null;
    const reportsCard = document.querySelector("#reports-card");
    if (reportsCard) {
      const reportStats = reportsCard.querySelector("#report-stats");
      const reportServices = reportsCard.querySelector("#report-services");
      const reportLabel = reportsCard.querySelector("#report-label");
      const reportButtons = [...reportsCard.querySelectorAll("[data-report-period]")];
      let activeReportPeriod = "day";
      async function loadReport(period) {
        activeReportPeriod = period;
        reportButtons.forEach((button) => {
          const selected = button.dataset.reportPeriod === period;
          button.classList.toggle("is-selected", selected);
          button.setAttribute("aria-pressed", String(selected));
        });
        try {
          const response = await api(`/api/providers/${encodeURIComponent(providerId)}/reports?period=${period}`);
          const report = response.report;
          reportLabel.textContent = report.label;
          reportStats.replaceChildren();
          const stats = [
            ["Customers served", report.served],
            ["Joined the queue", report.joined],
            ["No-shows", report.noShows],
            ["Removed", report.removed],
            ["Cleared at day end", report.clearedDayEnd],
            ["Average wait", `${report.avgWaitMinutes} min`],
            ["Average service", `${report.avgServiceMinutes} min`],
            ["Total service time", `${report.totalServiceMinutes} min`],
          ];
          for (const [label, value] of stats) {
            const box = document.createElement("div");
            box.className = "report-stat";
            appendText(box, "span", "", label);
            appendText(box, "strong", "", String(value));
            reportStats.append(box);
          }
          reportServices.replaceChildren();
          if (!report.services.length) {
            appendText(reportServices, "p", "report-empty", "No completed services in this period yet.");
          } else {
            const head = document.createElement("div");
            head.className = "report-service-row report-service-head";
            appendText(head, "span", "", "Service");
            appendText(head, "span", "", "Times done");
            appendText(head, "span", "", "Avg. time");
            reportServices.append(head);
            for (const service of report.services) {
              const row = document.createElement("div");
              row.className = "report-service-row";
              appendText(row, "span", "", service.name);
              appendText(row, "span", "", String(service.count));
              appendText(row, "span", "", service.avgMinutes === null ? "—" : `${service.avgMinutes} min`);
              reportServices.append(row);
            }
          }
        } catch (error) {
          showError(error.message);
        }
      }
      reportButtons.forEach((button) => {
        button.addEventListener("click", () => loadReport(button.dataset.reportPeriod));
      });
      loadReport("day");
      refreshReport = () => loadReport(activeReportPeriod);
    }

    const refreshDashboardButton = document.querySelector("#refresh-dashboard");
    if (refreshDashboardButton) {
      refreshDashboardButton.addEventListener("click", async () => {
        refreshDashboardButton.disabled = true;
        try {
          await refreshProviderQueue();
          if (refreshReport) await refreshReport();
        } finally {
          refreshDashboardButton.disabled = false;
        }
      });
    }
    window.setInterval(() => { if (refreshReport) refreshReport(); }, 300000);

    window.setInterval(refreshProviderQueue, 10000);
  }

  const adminDashboard = document.querySelector("[data-admin-dashboard]");
  if (adminDashboard) {
    const adminTabs = [...adminDashboard.querySelectorAll("[data-admin-tab]")];
    const adminPanels = [...adminDashboard.querySelectorAll("[data-admin-panel]")];
    function selectAdminTab(name) {
      adminTabs.forEach((tab) => {
        const selected = tab.dataset.adminTab === name;
        tab.classList.toggle("is-selected", selected);
        tab.setAttribute("aria-selected", String(selected));
      });
      adminPanels.forEach((panel) => {
        panel.hidden = panel.dataset.adminPanel !== name;
      });
      try {
        window.localStorage.setItem("smartqueue.adminTab", name);
      } catch (error) {
        // Storage unavailable; tabs still work for this visit.
      }
    }
    if (adminTabs.length) {
      adminTabs.forEach((tab) => {
        tab.addEventListener("click", () => selectAdminTab(tab.dataset.adminTab));
      });
      let initialTab = "approvals";
      try {
        const saved = window.localStorage.getItem("smartqueue.adminTab");
        if (adminTabs.some((tab) => tab.dataset.adminTab === saved)) initialTab = saved;
      } catch (error) {
        // Storage unavailable; start on Approvals.
      }
      selectAdminTab(initialTab);
    }
    const adminReports = adminDashboard.querySelector("[data-admin-reports]");
    if (adminReports) {
      const adminReportStats = adminReports.querySelector("#admin-report-stats");
      const adminReportLabel = adminReports.querySelector("#admin-report-label");
      const adminReportCategories = adminReports.querySelector("#admin-report-categories");
      const adminReportProviders = adminReports.querySelector("#admin-report-providers");
      const adminReportEmpty = adminReports.querySelector("#admin-report-empty");
      const adminReportButtons = [...adminReports.querySelectorAll("[data-admin-report-period]")];
      function adminAppendText(parent, tag, text) {
        const element = document.createElement(tag);
        element.textContent = text;
        parent.append(element);
        return element;
      }
      function adminReportRow(cells, markFirstAsName) {
        const row = document.createElement("tr");
        cells.forEach((cell, index) => {
          const td = document.createElement("td");
          if (markFirstAsName && index === 0) td.setAttribute("data-i18n-name", "");
          td.textContent = cell;
          row.append(td);
        });
        return row;
      }
      async function loadAdminReport(period) {
        adminReportButtons.forEach((button) => {
          const selected = button.dataset.adminReportPeriod === period;
          button.classList.toggle("is-selected", selected);
          button.setAttribute("aria-pressed", String(selected));
        });
        try {
          const response = await api(`/api/admin/reports?period=${period}`);
          const report = response.report;
          adminReportLabel.textContent = report.label;
          adminReportStats.replaceChildren();
          const totals = report.totals;
          const stats = [
            ["Customers served", totals.served],
            ["Joined the queue", totals.joined],
            ["No-shows", totals.noShows],
            ["Removed", totals.removed],
            ["Cleared at day end", totals.clearedDayEnd],
            ["Average wait", `${totals.avgWaitMinutes} min`],
            ["Average service", `${totals.avgServiceMinutes} min`],
            ["Total service time", `${totals.totalServiceMinutes} min`],
          ];
          for (const [label, value] of stats) {
            const box = document.createElement("div");
            box.className = "report-stat";
            adminAppendText(box, "span", label);
            adminAppendText(box, "strong", String(value));
            adminReportStats.append(box);
          }
          const healthMix = adminReports.querySelector("#admin-health-mix");
          const healthLegend = adminReports.querySelector("#admin-health-legend");
          const healthBars = adminReports.querySelector("#admin-health-bars");
          healthMix.replaceChildren();
          healthLegend.replaceChildren();
          healthBars.replaceChildren();
          const outcomes = [
            ["Served", totals.served, "seg-served"],
            ["No-shows", totals.noShows, "seg-noshow"],
            ["Removed", totals.removed, "seg-removed"],
            ["Cleared at day end", totals.clearedDayEnd, "seg-cleared"],
          ];
          const outcomeTotal = outcomes.reduce((sum, [, count]) => sum + count, 0);
          for (const [label, count, className] of outcomes) {
            if (!count || !outcomeTotal) continue;
            const segment = document.createElement("div");
            segment.className = className;
            segment.style.width = `${(count / outcomeTotal) * 100}%`;
            healthMix.append(segment);
            const item = document.createElement("span");
            item.className = "health-legend-item";
            const dot = document.createElement("i");
            dot.className = className;
            item.append(dot);
            item.append(document.createTextNode(`${label}: ${count} (${Math.round((count / outcomeTotal) * 100)}%)`));
            healthLegend.append(item);
          }
          if (!outcomeTotal) {
            const note = document.createElement("p");
            note.className = "report-empty";
            note.textContent = "No completed outcomes in this period yet.";
            healthBars.append(note);
          }
          const maxServed = Math.max(0, ...report.byProvider.map((row) => row.served));
          const activeProviders = report.byProvider
            .filter((row) => row.served || row.joined)
            .slice(0, 10);
          for (const row of activeProviders) {
            const barRow = document.createElement("div");
            barRow.className = "health-bar-row";
            const name = document.createElement("span");
            name.className = "health-bar-name";
            name.setAttribute("data-i18n-name", "");
            name.textContent = row.name;
            const track = document.createElement("div");
            track.className = "health-bar-track";
            const fill = document.createElement("div");
            fill.className = "health-bar-fill";
            fill.style.width = maxServed ? `${Math.max(3, (row.served / maxServed) * 100)}%` : "0%";
            track.append(fill);
            const value = document.createElement("span");
            value.className = "health-bar-value";
            value.textContent = `${row.served} served`;
            barRow.append(name, track, value);
            healthBars.append(barRow);
          }
          adminReportCategories.replaceChildren();
          for (const row of report.byCategory) {
            adminReportCategories.append(adminReportRow([
              row.category,
              String(row.providers),
              String(row.served),
              String(row.joined),
              String(row.noShows),
              String(row.removed),
              `${row.avgWaitMinutes} min`,
              `${row.avgServiceMinutes} min`,
            ], false));
          }
          adminReportProviders.replaceChildren();
          for (const row of report.byProvider) {
            adminReportProviders.append(adminReportRow([
              row.name,
              row.category,
              String(row.served),
              String(row.joined),
              String(row.noShows),
              String(row.removed),
              `${row.avgWaitMinutes} min`,
              `${row.avgServiceMinutes} min`,
            ], true));
          }
          adminReportEmpty.hidden = report.byProvider.length !== 0;
        } catch (error) {
          showError(error.message);
        }
      }
      adminReportButtons.forEach((button) => {
        button.addEventListener("click", () => loadAdminReport(button.dataset.adminReportPeriod));
      });
      loadAdminReport("day");
    }
    adminDashboard.addEventListener("click", async (event) => {
      const approvalButton = event.target.closest(".admin-approval-action");
      if (approvalButton) {
        const row = approvalButton.closest("[data-admin-provider-id]");
        const note = row.querySelector("[data-review-note]").value;
        approvalButton.disabled = true;
        try {
          await api(`/api/admin/providers/${encodeURIComponent(row.dataset.adminProviderId)}/approval`, {
            method: "PATCH",
            body: JSON.stringify({ action: approvalButton.dataset.action, note }),
          });
          window.location.reload();
        } catch (error) {
          approvalButton.disabled = false;
          showError(error.message);
        }
      }
    });
  }

  document.querySelector("#request-provider-review")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const providerId = document.querySelector(".dashboard-page[data-provider-id]")?.dataset.providerId;
    if (!providerId) return;
    button.disabled = true;
    try {
      await api(`/api/providers/${encodeURIComponent(providerId)}/request-review`, { method: "POST" });
      window.location.reload();
    } catch (error) {
      button.disabled = false;
      showError(error.message);
    }
  });

  const providerFields = document.querySelector("#provider-fields");
  if (providerFields) {
    const providerInputs = [...providerFields.querySelectorAll("input, select")];
    const accountTypeCards = [...document.querySelectorAll(".account-type-card")];
    document.querySelectorAll('input[name="role"]').forEach((input) => {
      input.addEventListener("change", () => {
        const isProvider = input.value === "provider" && input.checked;
        providerFields.hidden = !isProvider;
        providerInputs.forEach((field) => {
          field.disabled = !isProvider;
          field.required = isProvider && field.id !== "services";
        });
        accountTypeCards.forEach((card) => {
          card.classList.toggle("selected", card.querySelector('input[name="role"]').checked);
        });
      });
    });
    document.querySelector('input[name="role"]:checked')?.dispatchEvent(new Event("change"));
  }

  const photoInput = document.querySelector("#photo");
  if (photoInput) {
    const preview = document.querySelector("#photo-preview");
    const previewImage = document.querySelector("#photo-preview-image");
    const previewCaption = document.querySelector("#photo-preview-caption");
    const photoLabel = document.querySelector("#photo-label");
    const updatePhotoLabel = () => {
      photoLabel.firstChild.textContent = document.querySelector('input[name="role"]:checked')?.value === "provider"
        ? "Business photo "
        : "Profile photo ";
    };
    document.querySelectorAll('input[name="role"]').forEach((input) => {
      input.addEventListener("change", updatePhotoLabel);
    });
    updatePhotoLabel();
    photoInput.addEventListener("change", () => {
      const file = photoInput.files[0];
      if (!file) {
        preview.hidden = true;
        previewImage.removeAttribute("src");
        return;
      }
      if (previewImage.dataset.objectUrl) URL.revokeObjectURL(previewImage.dataset.objectUrl);
      const objectUrl = URL.createObjectURL(file);
      previewImage.dataset.objectUrl = objectUrl;
      previewImage.src = objectUrl;
      previewCaption.textContent = file.name;
      preview.hidden = false;
    });
  }
})();
