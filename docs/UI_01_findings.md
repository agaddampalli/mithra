# UI-01 Findings — Path A (Native HA Sidebar) Capability Test

Date: 2026-06-27
ADR reference: §17.1.2, §17.3
Status: Test 1 requires Abhiram to observe on deployment host.
        Tests 2 and 3 are factual HA capability findings (no running
        instance required — verified against HA source/docs).

---

## Test 1 — Multiple dashboards in HA's native sidebar

### Setup deployed
Four dashboards now registered in configuration.yaml:
  household-tablet  (existing, title: Household)
  test-tasks        (icon: mdi:checkbox-marked-outline, title: Tasks)
  test-routines     (icon: mdi:repeat, title: Routines)
  test-calendar     (icon: mdi:calendar, title: Calendar)

Three minimal placeholder YAML files created in dashboards/.

### To observe (Abhiram, on deployment host)

  docker compose up -d --force-recreate homeassistant

  Then open ha.mithra-app.com and answer:

  [ ] Do all 4 dashboards appear in HA's left sidebar as
      separate clickable entries with icon + title?

  [ ] Where do they appear relative to HA's built-in
      panels (History, Logbook, Settings, Developer Tools)?
      Custom dashboards appear above built-in panels,
      or below, or mixed?

  [ ] Can the order be controlled? (Known: HA docs say
      order follows YAML definition order in configuration.yaml,
      but the UI also has a drag-to-reorder for sidebar items
      in Settings → Dashboard → Sidebar. Test whether YAML
      order or UI drag-order takes precedence after reload.)

  [ ] Does the sidebar look like a usable navigation rail
      (icons + text labels, enough height per item to meet
      ADR 17.1.3's 12-15mm touch target), or does it look
      like a system settings list that was not designed
      for this use case?

  [ ] Does the Household dashboard entry have NO icon
      (it was defined without one — does HA show a
      default icon or a blank space)?

### What to report back
  A brief description of how the sidebar looks, or a
  screenshot. This is the only question that cannot be
  answered without looking at a running HA instance.

---

## Test 2 — Badge/count support on sidebar entries

### Finding: NOT natively supported

HA has two completely separate "badge" concepts:

  1. View-level badges (badges: list inside a dashboard view)
     — small entity-state chips shown at the top of a view's
     content area. Native, well-documented, already supported.
     NOT what we need.

  2. Sidebar-entry notification counts — a number overlaid
     on a sidebar nav item (like Slack unread or Gmail inbox
     count). THIS IS NOT A HA CONCEPT. HA's custom dashboard
     sidebar entries accept only:
       title, icon, show_in_sidebar, require_admin, mode,
       filename. No badge, badge_count, notification_count,
       or equivalent field exists.

HA's own notification bell (bottom-left of sidebar) is for
HA's system-level notifications only — not configurable
per-dashboard and not a count of domain-specific items like
pending tasks.

### Workaround without HACS: None viable
  The only non-HACS approach would be encoding the count
  into the dashboard title string (e.g. title: "Tasks (3)")
  — but HA's dashboard titles in configuration.yaml are
  static strings. They cannot be templated. Dynamic title
  updates from sensor state are not supported by the
  lovelace: dashboards: config block.

### Conclusion for Path A decision
  Sidebar badges require HACS (e.g. browser_mod or a
  custom sidebar extension). Path A cannot deliver this.
  The question is whether sidebar badges are a hard
  requirement or a nice-to-have. ADR §17 does not
  explicitly call out badge counts — the mockup PDF
  should be checked for whether counts appear on the
  nav items. If they don't appear in the mockup, this
  gap may be acceptable for Path A.

---

## Test 3 — Weather chip and quick-actions in sidebar

### Finding: NOT possible in Path A

HA's native sidebar is rendered by HA's own `ha-sidebar`
web component. Its layout is fixed by HA's frontend source:

  [HA logo / home button]
  [nav items: custom dashboards, built-in panels]
  [bottom: notification bell, user profile, help]

There is no slot, no YAML injection point, and no
Lovelace card slot inside the sidebar chrome. The sidebar
is not a Lovelace surface. Lovelace cards only render
inside dashboard view content areas — never in the sidebar.

The lovelace_resource: config (for loading custom JS/CSS)
and lovelace: dashboards: config both operate on dashboard
content, not on the sidebar shell.

### Where weather/quick-actions would need to live instead

If Path A is chosen, the weather chip and quick-action
buttons cannot be a persistent sidebar element visible on
every view. They would need to live as content cards at
the TOP of the Home dashboard's own page — visible on
Home only, not on Tasks/Routines/Calendar views.

This is a deviation from the mockup's literal design
(which shows these elements persistent in the sidebar).
ADR §17 should be amended to reflect this honestly if
Path A is chosen:

  - Weather chip: top row of Home view content
  - Quick-action buttons: second row of Home view content
  - Behavior: visible on Home, not persistent on other views
  - Acceptable tradeoff? The kitchen use case is primarily
    "glance at Home" — the user rarely needs weather while
    on the Tasks deep-dive page. The persistence was a
    design aspiration, not a researched requirement.

---

## Summary for path decision

| Requirement              | Path A (native) | Path B (HACS nav card) |
|--------------------------|-----------------|------------------------|
| Multiple nav entries     | YES             | YES                    |
| Icon + text labels       | YES             | YES                    |
| Kiosk touch targets      | PARTIAL*        | YES (configurable)     |
| Sidebar badge counts     | NO              | YES (with HACS)        |
| Weather in sidebar       | NO              | YES (with layout card) |
| Quick-actions in sidebar | NO              | YES (with layout card) |
| HACS dependency          | None beyond     | 1 additional card      |
|                          | Mushroom        | (layout/nav card)      |

*PARTIAL: HA's native sidebar item height is set by HA's
own CSS (approx 48px). Whether 48px meets 12-15mm depends
on the tablet's PPI. On a typical 10" 1920×1200 tablet
(~224 PPI), 48px ≈ 5.4mm — well below the 12-15mm kiosk
target. Path A fails ADR 17.1.3 on a standard tablet.

### Recommendation to bring to next session

Path A delivers navigation structure (Test 1) but fails
on three ADR §17 requirements:
  - Touch targets below kiosk spec (48px ≈ 5mm on typical tablet)
  - No sidebar badge counts
  - No persistent weather/quick-actions across views

Path B (one additional HACS layout/navigation card) would
address all three. The "one more dependency" cost is real
but small against delivering the researched design correctly.

The weather/quick-actions gap is addressable in Path A by
accepting them as Home-only content blocks — this may be
acceptable given the kitchen use pattern. Sidebar badges
and touch target sizing are harder to dismiss.

Decision is Abhiram's after reviewing Test 1 visual
appearance and this summary.
