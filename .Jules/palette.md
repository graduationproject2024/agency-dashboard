## 2025-10-21 - Department Buttons & Modal Controls Accessibility
**Learning:** Icon-only view buttons in the department list and modals lacked ARIA labels and focus visibility indicators for screen readers and keyboard users.
**Action:** Always complement icon-only actions with descriptive `aria-label` and `title` attributes, mark decorative icons with `aria-hidden="true"`, and provide explicit `:focus-visible` styling for interactive controls.
