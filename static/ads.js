(() => {
  "use strict";

  // YouAreNext advertisement module: one reusable display component used
  // by both placements (home hero, provider dashboard), plus the admin
  // management UI. A slot hides itself completely when no eligible ad
  // exists or every ad fails to load, so pages never show empty space.

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

  function api(path, options = {}) {
    return fetch(path, {
      ...options,
      headers: {
        ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
        ...(options.headers || {}),
      },
    });
  }

  async function apiJson(path, options = {}) {
    const response = await api(path, {
      ...options,
      headers: { "content-type": "application/json", ...(options.headers || {}) },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Something went wrong. Please try again.");
    return payload;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function reportEvent(context, ad, event) {
    if (context.preview) return;
    api("/api/ads/events", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        adId: ad.id,
        event,
        placement: context.placement,
        category: context.category || null,
        providerId: context.providerId || null,
      }),
      keepalive: true,
    }).catch(() => {});
  }

  function buildAdElement(ad) {
    const wrap = document.createElement("div");
    wrap.className = "ad-creative";
    const tag = document.createElement("span");
    tag.className = "ad-tag";
    tag.textContent = "Ad";
    wrap.appendChild(tag);
    if (ad.contentType === "text") {
      const text = document.createElement("p");
      text.className = "ad-text";
      text.textContent = ad.textContent || "";
      wrap.appendChild(text);
      return { wrap, media: null };
    }
    if (ad.contentType === "video") {
      const video = document.createElement("video");
      video.className = "ad-media";
      video.muted = true;
      video.autoplay = true;
      video.playsInline = true;
      video.preload = "auto";
      video.src = ad.mediaUrl;
      wrap.appendChild(video);
      return { wrap, media: video };
    }
    const img = document.createElement("img");
    img.className = "ad-media";
    img.alt = "Advertisement";
    img.src = ad.mediaUrl;
    wrap.appendChild(img);
    return { wrap, media: img };
  }

  class AdSlot {
    constructor(element, context) {
      this.element = element;
      this.context = context;
      this.ads = [];
      this.index = 0;
      this.timer = null;
      this.current = null;
      this.observer = null;
    }

    async load(ads) {
      if (Array.isArray(ads)) {
        this.setAds(ads);
        return;
      }
      const params = new URLSearchParams({ placement: this.context.placement });
      if (this.context.category) params.set("category", this.context.category);
      if (this.context.providerId) params.set("providerId", this.context.providerId);
      try {
        const response = await fetch(`/api/ads/eligible?${params.toString()}`);
        if (!response.ok) throw new Error("Advertisements unavailable.");
        const payload = await response.json();
        this.setAds(Array.isArray(payload.ads) ? payload.ads : []);
      } catch {
        this.setAds([]);
      }
    }

    refresh(context) {
      this.context = { ...this.context, ...context };
      return this.load();
    }

    setAds(ads) {
      this.stopTimer();
      this.ads = ads.slice();
      this.index = 0;
      if (!this.ads.length) {
        this.hide();
        return;
      }
      this.showCurrent();
    }

    hide() {
      this.stopTimer();
      this.teardownCurrent();
      this.element.replaceChildren();
      this.element.hidden = true;
    }

    stopTimer() {
      if (this.timer) {
        clearTimeout(this.timer);
        this.timer = null;
      }
    }

    teardownCurrent() {
      if (this.observer) {
        this.observer.disconnect();
        this.observer = null;
      }
      const media = this.current?.media;
      if (media && media.tagName === "VIDEO") {
        try {
          media.pause();
          media.removeAttribute("src");
          media.load();
        } catch {
          /* a detached video needs no cleanup guarantees */
        }
      }
      this.current = null;
    }

    showCurrent() {
      if (!this.ads.length) {
        this.hide();
        return;
      }
      const ad = this.ads[this.index % this.ads.length];
      this.teardownCurrent();
      const { wrap, media } = buildAdElement(ad);
      this.element.replaceChildren(wrap);
      this.element.hidden = false;
      const current = { ad, media, completed: false };
      this.current = current;
      reportEvent(this.context, ad, "started");
      if (media) {
        media.addEventListener("error", () => this.failCurrent(current), { once: true });
        if (media.tagName === "VIDEO") {
          const attempt = media.play?.();
          if (attempt) attempt.catch(() => {});
          this.observeVisibility(media);
        }
      }
      const durationMs = Math.max(3, Number(ad.durationSeconds) || 8) * 1000;
      this.timer = setTimeout(() => {
        if (this.current === current && !current.completed) {
          current.completed = true;
          reportEvent(this.context, ad, "completed");
        }
        this.next();
      }, durationMs);
      this.preloadNext();
    }

    failCurrent(current) {
      if (this.current !== current) return;
      reportEvent(this.context, current.ad, "failed");
      this.ads = this.ads.filter((item) => item.id !== current.ad.id);
      this.stopTimer();
      if (!this.ads.length) {
        this.hide();
        return;
      }
      this.index = this.index % this.ads.length;
      this.showCurrent();
    }

    next() {
      if (!this.ads.length) return;
      this.index = (this.index + 1) % this.ads.length;
      this.showCurrent();
    }

    observeVisibility(video) {
      if (!("IntersectionObserver" in window)) return;
      this.observer = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (entry.target !== video) continue;
            if (entry.isIntersecting) {
              const attempt = video.play?.();
              if (attempt) attempt.catch(() => {});
            } else {
              video.pause();
            }
          }
        },
        { threshold: 0.25 }
      );
      this.observer.observe(video);
    }

    preloadNext() {
      if (this.ads.length < 2) return;
      const next = this.ads[(this.index + 1) % this.ads.length];
      if (next.contentType === "image" && next.mediaUrl) {
        const img = new Image();
        img.src = next.mediaUrl;
      }
    }
  }

  // ---- Placements -------------------------------------------------------

  const homeSlotElement = document.querySelector('[data-ad-placement="home"]');
  if (homeSlotElement) {
    const selectedCategory = () => {
      const chip = document.querySelector(".category-chip.selected");
      const value = chip?.dataset.category;
      return value && value !== "All services" && value !== "Favorites" ? value : null;
    };
    const homeSlot = new AdSlot(homeSlotElement, {
      placement: "home",
      category: selectedCategory(),
    });
    homeSlot.load();
    document.querySelector(".category-row")?.addEventListener("click", (event) => {
      if (!event.target.closest(".category-chip")) return;
      // app.js updates the selected chip in its own handler, which was
      // registered first; defer so we read the new selection.
      setTimeout(() => homeSlot.refresh({ category: selectedCategory() }), 0);
    });
  }

  const dashboardSlotElement = document.querySelector('[data-ad-placement="dashboard"]');
  if (dashboardSlotElement) {
    const dashboardSlot = new AdSlot(dashboardSlotElement, {
      placement: "dashboard",
      providerId: dashboardSlotElement.dataset.adProvider || null,
      category: dashboardSlotElement.dataset.adCategory || null,
    });
    dashboardSlot.load();
  }

  // ---- Provider permission toggle (admin provider list) -----------------

  const adminDashboard = document.querySelector("[data-admin-dashboard]");
  if (adminDashboard) {
    adminDashboard.addEventListener("change", async (event) => {
      const toggle = event.target.closest("[data-ads-permission]");
      if (!toggle) return;
      const row = toggle.closest("[data-admin-provider-id]");
      if (!row) return;
      const notice = document.querySelector("#admin-approval-notice");
      try {
        await apiJson(
          `/api/admin/providers/${encodeURIComponent(row.dataset.adminProviderId)}/ads-permission`,
          { method: "PATCH", body: JSON.stringify({ adsEnabled: toggle.checked }) }
        );
        if (notice) {
          notice.hidden = false;
          notice.textContent = toggle.checked
            ? "Advertisements enabled for this provider."
            : "Advertisements disabled for this provider.";
        }
      } catch (error) {
        toggle.checked = !toggle.checked;
        if (notice) {
          notice.hidden = false;
          notice.textContent = error.message;
        }
      }
    });
  }

  // ---- Admin advertisement management -----------------------------------

  const adminRoot = document.querySelector("[data-ads-admin]");
  if (adminRoot) initAdsAdmin(adminRoot);

  function initAdsAdmin(root) {
    const tbody = root.querySelector("#ads-tbody");
    const emptyNote = root.querySelector("#ads-empty");
    const notice = root.querySelector("#ads-notice");
    const form = root.querySelector("#ad-form");
    const formTitle = root.querySelector("#ad-form-title");
    const previewWrap = root.querySelector("#ad-preview-wrap");
    const previewFrame = root.querySelector("#ad-preview-frame");
    let adsCache = [];
    let previewSlot = null;
    let previewObjectUrl = null;

    function showNotice(message, isError = false) {
      notice.hidden = false;
      notice.textContent = message;
      notice.classList.toggle("error", isError);
    }

    function formatDate(value) {
      if (!value) return "—";
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
    }

    function renderTable(ads) {
      adsCache = ads;
      tbody.replaceChildren();
      emptyNote.hidden = ads.length > 0;
      for (const ad of ads) {
        const row = document.createElement("tr");
        row.innerHTML = `
          <td><strong>${escapeHtml(ad.name)}</strong></td>
          <td>${escapeHtml(ad.contentType)}</td>
          <td>${escapeHtml((ad.categories || []).join(", "))}</td>
          <td>${escapeHtml((ad.placements || []).join(", "))}</td>
          <td>${escapeHtml(ad.durationSeconds)}s</td>
          <td>${escapeHtml(formatDate(ad.startsAt))} → ${escapeHtml(formatDate(ad.endsAt))}</td>
          <td><span class="table-status ${ad.active ? "next-up" : "rejected"}">${ad.active ? "Active" : "Inactive"}</span></td>
          <td class="ads-row-actions">
            <button class="button button-outline" type="button" data-ad-action="preview">Preview</button>
            <button class="button button-outline" type="button" data-ad-action="edit">Edit</button>
            <button class="button button-outline" type="button" data-ad-action="delete">Delete</button>
          </td>`;
        row.dataset.adId = ad.id;
        tbody.appendChild(row);
      }
    }

    async function loadAds() {
      const params = new URLSearchParams();
      const status = root.querySelector("#ads-filter-status").value;
      const category = root.querySelector("#ads-filter-category").value;
      const type = root.querySelector("#ads-filter-type").value;
      if (status) params.set("status", status);
      if (category) params.set("category", category);
      if (type) params.set("type", type);
      try {
        const payload = await apiJson(`/api/admin/ads?${params.toString()}`);
        renderTable(payload.ads || []);
      } catch (error) {
        showNotice(error.message, true);
      }
    }

    function toLocalInput(value) {
      if (!value) return "";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return "";
      const pad = (part) => String(part).padStart(2, "0");
      return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
    }

    function syncTypeFields() {
      const type = form.elements.contentType.value;
      form.querySelector("#ad-file-row").hidden = type === "text";
      form.querySelector("#ad-text-row").hidden = type !== "text";
      const fileInput = form.elements.media;
      fileInput.accept =
        type === "video" ? "video/mp4,video/webm" : "image/jpeg,image/png,image/webp";
      fileInput.required = type !== "text" && !form.elements.id.value;
    }

    function openForm(ad) {
      form.reset();
      form.elements.id.value = ad?.id || "";
      formTitle.textContent = ad ? "Edit advertisement" : "New advertisement";
      form.elements.name.value = ad?.name || "";
      form.elements.description.value = ad?.description || "";
      form.elements.contentType.value = ad?.contentType || "image";
      form.elements.textContent.value = ad?.textContent || "";
      form.elements.durationSeconds.value = ad?.durationSeconds ?? 8;
      form.elements.priority.value = ad?.priority ?? 0;
      form.elements.active.checked = ad ? ad.active === true : true;
      form.elements.startsAt.value = toLocalInput(ad?.startsAt);
      form.elements.endsAt.value = toLocalInput(ad?.endsAt);
      for (const box of form.querySelectorAll('input[name="ad-category"]')) {
        box.checked = ad ? (ad.categories || []).includes(box.value) : false;
      }
      for (const box of form.querySelectorAll('input[name="ad-placement"]')) {
        box.checked = ad ? (ad.placements || []).includes(box.value) : box.value === "home";
      }
      const fileNote = form.querySelector("#ad-file-note");
      fileNote.textContent =
        ad?.mediaFileName
          ? `Current file: ${ad.mediaFileName} — choose a new file to replace it.`
          : "";
      syncTypeFields();
      form.hidden = false;
      form.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function closeForm() {
      form.hidden = true;
    }

    function collectForm() {
      const data = new FormData();
      const id = form.elements.id.value;
      data.set("name", form.elements.name.value);
      data.set("description", form.elements.description.value);
      data.set("contentType", form.elements.contentType.value);
      data.set("textContent", form.elements.textContent.value);
      data.set("durationSeconds", form.elements.durationSeconds.value);
      data.set("priority", form.elements.priority.value);
      data.set("active", form.elements.active.checked ? "true" : "false");
      const starts = form.elements.startsAt.value;
      const ends = form.elements.endsAt.value;
      data.set("startsAt", starts ? new Date(starts).toISOString() : "");
      data.set("endsAt", ends ? new Date(ends).toISOString() : "");
      data.set(
        "categories",
        JSON.stringify(
          [...form.querySelectorAll('input[name="ad-category"]:checked')].map((box) => box.value)
        )
      );
      data.set(
        "placements",
        JSON.stringify(
          [...form.querySelectorAll('input[name="ad-placement"]:checked')].map((box) => box.value)
        )
      );
      const file = form.elements.media.files?.[0];
      if (file) data.set("media", file);
      return { id, data };
    }

    function publicShape(source, mediaUrlOverride) {
      return {
        id: source.id || "preview",
        contentType: source.contentType,
        textContent: source.textContent || "",
        durationSeconds: Number(source.durationSeconds) || 8,
        mediaUrl: mediaUrlOverride || (source.mediaId ? `/media/ads/${source.mediaId}` : null),
      };
    }

    function showPreview(adShape) {
      const placement = root.querySelector("#ad-preview-placement").value;
      const device = root.querySelector("#ad-preview-device").value;
      previewFrame.className = `ad-preview-frame ${device === "mobile" ? "is-mobile" : ""}`;
      previewFrame.replaceChildren();
      const slotElement = document.createElement("div");
      slotElement.className = `ad-slot ad-slot-${placement}`;
      previewFrame.appendChild(slotElement);
      previewSlot = new AdSlot(slotElement, { placement, preview: true });
      previewSlot.setAds([adShape]);
      previewWrap.hidden = false;
      previewWrap.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    root.querySelector("#ads-new").addEventListener("click", () => openForm(null));
    root
      .querySelector("#ad-form-cancel")
      .addEventListener("click", () => closeForm());
    form.elements.contentType.addEventListener("change", syncTypeFields);
    for (const filterId of ["#ads-filter-status", "#ads-filter-category", "#ads-filter-type"]) {
      root.querySelector(filterId).addEventListener("change", loadAds);
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const { id, data } = collectForm();
      try {
        const payload = await api(id ? `/api/admin/ads/${encodeURIComponent(id)}` : "/api/admin/ads", {
          method: id ? "PATCH" : "POST",
          body: data,
        }).then(async (response) => {
          const body = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(body.error || "Something went wrong. Please try again.");
          return body;
        });
        showNotice(id ? "Advertisement updated." : "Advertisement created.");
        closeForm();
        await loadAds();
        return payload;
      } catch (error) {
        showNotice(error.message, true);
      }
    });

    root.querySelector("#ad-form-preview").addEventListener("click", () => {
      const { data } = collectForm();
      const shape = {};
      for (const [key, value] of data.entries()) {
        if (key !== "media") shape[key] = value;
      }
      const file = form.elements.media.files?.[0];
      let mediaUrl = null;
      if (file) {
        if (previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
        previewObjectUrl = URL.createObjectURL(file);
        mediaUrl = previewObjectUrl;
      } else {
        const existing = adsCache.find((ad) => ad.id === form.elements.id.value);
        if (existing) mediaUrl = existing.mediaId ? `/media/ads/${existing.mediaId}` : null;
      }
      showPreview(publicShape(shape, mediaUrl));
    });

    tbody.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-ad-action]");
      if (!button) return;
      const row = button.closest("tr[data-ad-id]");
      const ad = adsCache.find((item) => item.id === row?.dataset.adId);
      if (!ad) return;
      const action = button.dataset.adAction;
      if (action === "edit") openForm(ad);
      if (action === "preview") showPreview(publicShape(ad));
      if (action === "delete") {
        if (!window.confirm(`Delete the advertisement “${ad.name}”?`)) return;
        try {
          await apiJson(`/api/admin/ads/${encodeURIComponent(ad.id)}`, { method: "DELETE" });
          showNotice("Advertisement deleted.");
          await loadAds();
        } catch (error) {
          showNotice(error.message, true);
        }
      }
    });

    root.querySelector("#ad-preview-close").addEventListener("click", () => {
      previewWrap.hidden = true;
      previewFrame.replaceChildren();
      previewSlot = null;
    });
    root.querySelector("#ad-preview-placement").addEventListener("change", () => {
      if (!previewWrap.hidden && previewSlot?.current) {
        showPreview(previewSlot.current.ad);
      }
    });
    root.querySelector("#ad-preview-device").addEventListener("change", (event) => {
      previewFrame.classList.toggle("is-mobile", event.target.value === "mobile");
    });

    loadAds();
  }
})();
