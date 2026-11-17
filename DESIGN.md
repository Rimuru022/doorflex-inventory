# Design

## Theme

Light. Nick at his desk mid-morning, Arizona sun through the window, scanning inventory on a 24-inch monitor. Light mode is the one mode. This is an office tool, not a late-night terminal.

## Color

### Strategy

Restrained. Tinted warm neutrals as the foundation, one orange accent pulled from the DoorFlex brand, used sparingly for primary actions and key state indicators.

### Palette

```oklch
--color-bg:       oklch(0.97 0.005 50)    /* warm off-white page background */
--color-surface:  oklch(0.995 0.003 50)   /* near-white card/section surface */
--color-border:   oklch(0.88 0.006 50)    /* soft warm border */
--color-text:     oklch(0.15 0.01 50)     /* dark warm tone for body */
--color-muted:    oklch(0.55 0.012 50)    /* secondary text, labels */
--color-accent:   oklch(0.62 0.16 55)     /* DoorFlex orange: primary actions, links */
--color-accent-hover: oklch(0.57 0.17 55) /* darker orange for hover */
--color-success:  oklch(0.55 0.14 145)    /* green for stock OK status */
--color-danger:   oklch(0.55 0.18 30)     /* red for low stock, errors */
--color-warning:  oklch(0.65 0.14 85)     /* amber for attention states */
--color-nav-bg:   oklch(0.14 0.012 50)    /* dark warm nav background */
--color-nav-text: oklch(0.85 0.008 50)    /* light text on nav */
--color-nav-accent: oklch(0.62 0.16 55)   /* orange for active nav item */
```

### Usage

- Accent orange reserved for primary buttons, active nav item, interactive hover states, links. Not for decoration.
- Green/red/amber are semantic only: stock status, alerts.
- Borders are barely visible (0.5px or 1px, low chroma). Let whitespace create separation, not lines.

## Typography

### Stack

```
font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
font-feature-settings: "tnum";  /* tabular numbers in tables */
```

Single family. No display face. System fonts keep it fast and native.

### Scale

```
--text-xs:   0.75rem  (400)   /* table data, metadata */
--text-sm:   0.8125rem (400)  /* labels, secondary */
--text-base: 0.9375rem (400)  /* body */
--text-lg:   1.125rem  (600)  /* subsection headings */
--text-xl:   1.5rem    (600)  /* page titles */
--text-2xl:  2rem      (700)  /* summary numbers */
```

### Measure

Body width capped at 70ch. Data tables exempt (can run full width).

## Layout

### Shell

- Full-width dark top bar with logo and nav links.
- Content area has generous horizontal padding (2-3rem) and vertical rhythm.
- No sidebar. Single-level horizontal nav is enough for the scope.

### Spacing rhythm

```
--space-xs:  0.25rem
--space-sm:  0.5rem
--space-md:  1rem
--space-lg:  1.5rem
--space-xl:  2rem
--space-2xl: 3rem
```

Articles (sections) get `--space-xl` bottom margin. Tables inside articles have `--space-md` above.

### Density

Tables are the primary data display. Rows at 2.25rem height. Hover highlight on rows for scanability. Alternating row backgrounds for long tables.

## Components

### Buttons

Primary: filled orange (`--color-accent`), white text, 6px radius.
Secondary: outlined, `--color-text` border, transparent bg.
Danger: red tint, white text, for destructive actions.

All hover states darken by 5% lightness. Active state pushes 10%. Disabled at 40% opacity.

### Tables

Full-width, no vertical cell borders. Horizontal dividers only (1px `--color-border`). Header row has 600 weight, `--text-sm` uppercase label. Rows highlight on hover (`--color-bg` tint). Stock status uses both icon and text (🔴 Low / ✅ OK), not color alone.

### Forms

Standard stacked labels above inputs. Inputs at 2.5rem height for comfortable click targets. Focus ring uses `--color-accent` at 0.15 spread. Selects styled the same as text inputs (Pico CSS default is acceptable with color overrides).

### Navigation

Dark bar (`--color-nav-bg`), left-aligned logo, right-aligned links. Active page gets orange underline or orange text. Hover state lightens text slightly. Not sticky on mobile; sticky on desktop.

## Component bans

- No cards as the default container. Use sections with headers and direct child tables.
- No gradient text, no glassmorphism, no side-stripe borders.
- No decorative icons next to every label. Icons only when they add scan value (status, actions).
