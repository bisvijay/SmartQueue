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

    document.querySelectorAll(".join-queue").forEach((button) => button.addEventListener("click", async () => {
      try {
        await api("/api/queues", {
          method: "POST",
          body: JSON.stringify({ providerId: button.dataset.providerId }),
        });
        window.location.reload();
      } catch (error) {
        showError(error.message);
      }
    }));

    document.querySelector("#leave-queue")?.addEventListener("click", async (event) => {
      const queueId = event.currentTarget.dataset.queueId;
      try {
        await api(`/api/queues/${encodeURIComponent(queueId)}`, {
          method: "DELETE",
        });
        window.location.reload();
      } catch (error) {
        showError(error.message);
      }
    });

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

    if (document.querySelector("#queue-position")) {
      const refreshQueue = async () => {
        try {
          const current = await api("/api/queues/current");
          if (!current) {
            window.location.reload();
            return;
          }
          document.querySelector("#queue-position").textContent = current.position;
          document.querySelector("#queue-wait").textContent = current.wait;
          if (current.status === "serving") {
            document.querySelector("#queue-state").textContent = "YOU’RE BEING SERVED";
            document.querySelector("#leave-queue")?.remove();
          }
        } catch (error) {
          showError(error.message);
        }
      };
      window.setInterval(refreshQueue, 15000);
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
    const presentationButton = document.querySelector("#toggle-presentation");
    const queueFilterHeading = document.querySelector(".queue-table-heading span:nth-child(2)");
    let activeQueueFilter = "active";
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

    function renderQueue(queues, filter = activeQueueFilter) {
      queueRows.replaceChildren();
      const showingCompleted = filter === "completed";
      const serving = queues.some((entry) => entry.status === "serving");
      for (const [index, entry] of queues.entries()) {
        const row = document.createElement("div");
        row.className = "queue-table-row";
        row.dataset.queueId = entry.id;
        const customer = document.createElement("div");
        customer.className = "customer-cell";
        appendText(customer, "span", `customer-avatar avatar-${index % 5 + 1}`, (entry.customerName || "Customer").slice(0, 2).toUpperCase());
        const customerText = document.createElement("span");
        appendText(customerText, "strong", "", entry.customerName || "Customer");
        const details = [entry.customerPhone, entry.customerEmail, entry.customerAddress, entry.customerService].filter(Boolean).join(" · ")
          || (entry.status === "serving" ? "Service in progress" : "Customer");
        appendText(customerText, "small", "customer-contact", details);
        customer.append(customerText);
        row.append(customer);
        appendText(row, "span", "wait-cell", showingCompleted ? formatCompletedAt(entry.completedAt) : entry.status === "serving" ? "Now" : `~${entry.estimatedWait} min`);
        const action = document.createElement("span");
        action.className = "queue-row-action";
        if (showingCompleted) {
          appendText(action, "span", "table-status completed", "Completed");
        } else if (entry.status === "serving") {
          appendText(action, "span", "table-status next-up", "Serving");
          const button = appendText(action, "button", "button button-complete queue-action", "Complete");
          button.type = "button";
          button.dataset.action = "complete";
          button.dataset.queueId = entry.id;
        } else {
          appendText(action, "span", "table-status waiting", index === 0 ? "Next up" : "Waiting");
          const button = appendText(action, "button", "button button-outline queue-action", "Start service");
          button.type = "button";
          button.dataset.action = "serve";
          button.dataset.queueId = entry.id;
          button.disabled = serving;
        }
        row.append(action);
        queueRows.append(row);
      }
      noWaiting.textContent = showingCompleted ? "No completed services yet." : "No one waiting. Enjoy the breather.";
      noWaiting.hidden = queues.length !== 0;
      queueCount.textContent = queues.length;
      const countLabel = queueCount.parentElement;
      countLabel.replaceChildren(queueCount);
      appendText(countLabel, "span", "", showingCompleted
        ? ` completed service${queues.length === 1 ? "" : "s"}`
        : ` customer${queues.length === 1 ? "" : "s"} in your queue`);
      queueFilterHeading.textContent = showingCompleted ? "COMPLETED" : "WAIT";
      callNextButton.hidden = showingCompleted;
      if (!showingCompleted) {
        peopleMetric.textContent = queues.length;
        waitMetric.textContent = `${queues.length * serviceDuration} min`;
        callNextButton.disabled = queues.length === 0 || serving;
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
    document.querySelector("#walk-in-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const values = new FormData(form);
      const submit = form.querySelector('button[type="submit"]');
      submit.disabled = true;
      try {
        await api(`/api/providers/${encodeURIComponent(providerId)}/walk-ins`, {
          method: "POST",
          body: JSON.stringify({
            name: values.get("name"),
            email: values.get("email"),
            phone: values.get("phone"),
            address: values.get("address"),
            service: values.get("service"),
          }),
        });
        form.reset();
        selectQueueFilter("active");
        await refreshProviderQueue();
      } catch (error) {
        showError(error.message);
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
          }),
        });
        window.location.reload();
      } catch (error) {
        showError(error.message);
      }
    });
    window.setInterval(refreshProviderQueue, 10000);
  }

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
