"""HTML, CSS, and JS template generator for Agent Manifest UI.

Adheres strictly to the Algen Agent Studio platform design system with zero emojis
and exclusively uses precision vector SVG icons.
"""

from __future__ import annotations

import json
from typing import Any

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>__PAGE_TITLE__ - Algen Agent Runtime</title>
  <style>
    :root {
      --ink: #111827;
      --page: #f8fafc;
      --muted: #64748b;
      --divider: #e2e8f0;
      --surface-2: #f1f5f9;
      --paper: #ffffff;
      --brand: #7c3aed;
      --brand-dark: #6d28d9;
      --brand-pale: #f5f3ff;
      --brand-cyan: #06b6d4;
      --brand-pink: #ec4899;
      --rust: var(--brand);
      --rust-dark: var(--brand-dark);
      --rust-pale: var(--brand-pale);
      --success: #247451;
      --success-pale: #eaf5f0;
      --warning: #9a6208;
      --warning-pale: #fef8eb;
      --error: #c53030;
      --error-pale: #fef2f2;
      
      --font-sans: 'Manrope', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      --font-mono: 'IBM Plex Mono', 'Menlo', 'Monaco', 'Courier New', monospace;

      --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
      --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.08), 0 2px 4px -2px rgba(0, 0, 0, 0.04);
      --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -4px rgba(0, 0, 0, 0.05);

      --badge-agent-bg: #f5f3ff;
      --badge-agent-text: #6d28d9;
      --badge-agent-border: #ddd6fe;

      --badge-wf-bg: #ecfeff;
      --badge-wf-text: #0e7490;
      --badge-wf-border: #a5f3fc;

      --badge-handler-bg: #ecfdf5;
      --badge-handler-text: #047857;
      --badge-handler-border: #a7f3d0;

      --badge-approval-bg: #fffbeb;
      --badge-approval-text: #b45309;
      --badge-approval-border: #fde68a;

      --badge-join-bg: #fdf4ff;
      --badge-join-text: #a21caf;
      --badge-join-border: #f5d0fe;

      --code-bg: #0f172a;
      --code-text: #e2e8f0;
    }

    [data-theme="dark"] {
      --ink: #f8fafc;
      --page: #0b0f19;
      --muted: #94a3b8;
      --divider: #1e293b;
      --surface-2: #161f33;
      --paper: #111827;
      --brand: #8b5cf6;
      --brand-dark: #7c3aed;
      --brand-pale: rgba(139, 92, 246, 0.12);
      --brand-cyan: #22d3ee;
      --rust: var(--brand);
      --rust-dark: var(--brand-dark);
      --rust-pale: var(--brand-pale);
      --success: #34d399;
      --success-pale: rgba(52, 211, 153, 0.12);
      --warning: #fbbf24;
      --warning-pale: rgba(251, 191, 36, 0.12);
      --error: #f87171;
      --error-pale: rgba(248, 113, 113, 0.12);

      --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.3);
      --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.4), 0 2px 4px -2px rgba(0, 0, 0, 0.3);
      --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.5), 0 4px 6px -4px rgba(0, 0, 0, 0.4);

      --badge-agent-bg: rgba(139, 92, 246, 0.15);
      --badge-agent-text: #c4b5fd;
      --badge-agent-border: rgba(139, 92, 246, 0.3);

      --badge-wf-bg: rgba(6, 182, 212, 0.15);
      --badge-wf-text: #67e8f9;
      --badge-wf-border: rgba(6, 182, 212, 0.3);

      --badge-handler-bg: rgba(16, 185, 129, 0.15);
      --badge-handler-text: #6ee7b7;
      --badge-handler-border: rgba(16, 185, 129, 0.3);

      --badge-approval-bg: rgba(245, 158, 11, 0.15);
      --badge-approval-text: #fcd34d;
      --badge-approval-border: rgba(245, 158, 11, 0.3);

      --badge-join-bg: rgba(168, 85, 247, 0.15);
      --badge-join-text: #d8b4fe;
      --badge-join-border: rgba(168, 85, 247, 0.3);

      --code-bg: #070a12;
      --code-text: #f1f5f9;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      font-family: var(--font-sans);
      background-color: var(--page);
      color: var(--ink);
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
      padding-bottom: 60px;
    }

    /* Top Navigation Bar - Algen Studio Shell */
    header.top-bar {
      height: 54px;
      background-color: #111827;
      color: #ffffff;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 24px;
      position: sticky;
      top: 0;
      z-index: 100;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25);
    }

    .studio-brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }

    .studio-brand-logo-badge {
      width: 28px;
      height: 28px;
      border-radius: 6px;
      background: linear-gradient(135deg, #7c3aed, #06b6d4);
      color: #ffffff;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      box-shadow: 0 2px 8px rgba(124, 58, 237, 0.35);
    }

    .studio-brand-text {
      display: flex;
      align-items: baseline;
      gap: 6px;
    }

    .studio-brand-title {
      font-weight: 800;
      font-size: 0.9375rem;
      letter-spacing: 0.04em;
      color: #ffffff;
    }

    .studio-brand-subtitle {
      font-size: 0.6875rem;
      font-family: var(--font-mono);
      color: #c4b5fd;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }

    .studio-brand-tagline {
      padding-left: 10px;
      border-left: 1px solid rgba(196, 181, 253, 0.25);
      color: #94a3b8;
      font: 500 0.6875rem var(--font-mono);
      letter-spacing: 0.02em;
      white-space: nowrap;
    }

    .top-bar-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    /* Professional Search Box */
    .search-box {
      position: relative;
      display: flex;
      align-items: center;
    }

    .search-box input {
      background: rgba(255, 255, 255, 0.07);
      border: 1px solid rgba(255, 255, 255, 0.14);
      color: #ffffff;
      font-size: 0.8125rem;
      padding: 6px 12px 6px 32px;
      border-radius: 4px;
      width: 220px;
      transition: all 0.15s ease;
      font-family: inherit;
    }

    .search-box input:focus {
      outline: none;
      background: rgba(255, 255, 255, 0.12);
      border-color: #8b5cf6;
      box-shadow: 0 0 0 2px rgba(139, 92, 246, 0.25);
      width: 280px;
    }

    .search-box input::placeholder {
      color: #64748b;
    }

    .search-icon {
      position: absolute;
      left: 10px;
      color: #94a3b8;
      display: flex;
      align-items: center;
      pointer-events: none;
    }

    /* Buttons */
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      height: 32px;
      padding: 0 12px;
      border-radius: 4px;
      font-weight: 600;
      font-size: 0.8125rem;
      cursor: pointer;
      border: 1px solid transparent;
      white-space: nowrap;
      user-select: none;
      transition: background-color 0.15s, border-color 0.15s, color 0.15s;
    }

    .btn-topbar {
      background-color: rgba(255, 255, 255, 0.08);
      color: #e2e8f0;
      border-color: rgba(255, 255, 255, 0.14);
    }

    .btn-topbar:hover {
      background-color: rgba(255, 255, 255, 0.15);
      color: #ffffff;
    }

    .btn-primary {
      background-color: var(--brand);
      color: #ffffff;
      border-color: var(--brand);
    }

    .btn-primary:hover:not(:disabled) {
      background-color: var(--brand-dark);
    }

    .btn-secondary {
      background-color: var(--paper);
      color: var(--ink);
      border-color: var(--divider);
    }

    .btn-secondary:hover:not(:disabled) {
      background-color: var(--surface-2);
      border-color: var(--muted);
    }

    .btn-icon {
      width: 32px;
      height: 32px;
      padding: 0;
      border-radius: 4px;
      background-color: transparent;
      color: var(--muted);
      border: 1px solid var(--divider);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
    }

    .btn-icon:hover {
      background-color: var(--surface-2);
      color: var(--ink);
    }

    /* Studio Workspace Subheader */
    .workspace-header {
      padding: 14px 28px;
      background-color: var(--paper);
      border-bottom: 1px solid var(--divider);
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
    }

    .workspace-breadcrumb {
      font-family: var(--font-mono);
      font-size: 0.6875rem;
      color: var(--muted);
      margin-bottom: 3px;
      letter-spacing: 0.02em;
    }

    .workspace-title-row {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .workspace-title {
      font-size: 1.1875rem;
      font-weight: 700;
      color: var(--ink);
      letter-spacing: -0.01em;
    }

    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 2px 8px;
      border-radius: 3px;
      font-family: var(--font-mono);
      font-size: 0.6875rem;
      font-weight: 600;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }

    .status-badge-live {
      background-color: var(--success-pale);
      color: var(--success);
      border: 1px solid var(--success);
    }

    .status-badge-static {
      background-color: var(--surface-2);
      color: var(--muted);
      border: 1px solid var(--divider);
    }

    /* Product Tabs - Studio Style */
    .product-tabs-bar {
      height: 44px;
      background-color: var(--paper);
      border-bottom: 1px solid var(--divider);
      display: flex;
      align-items: stretch;
      padding: 0 28px;
      gap: 16px;
      overflow-x: auto;
      flex-shrink: 0;
    }

    .tab-btn {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 0 4px;
      background: none;
      border: none;
      border-bottom: 2px solid transparent;
      color: var(--muted);
      font-size: 0.8125rem;
      font-weight: 500;
      cursor: pointer;
      transition: color 0.12s ease, border-color 0.12s ease;
      white-space: nowrap;
      user-select: none;
    }

    .tab-btn:hover {
      color: var(--ink);
    }

    .tab-btn.active {
      color: var(--brand);
      border-bottom-color: var(--brand);
      font-weight: 700;
    }

    .tab-step {
      font-family: var(--font-mono);
      font-size: 0.65625rem;
      padding: 1px 5px;
      border-radius: 3px;
      background-color: var(--surface-2);
      color: var(--muted);
      font-weight: 600;
    }

    .tab-btn.active .tab-step {
      background-color: var(--brand-pale);
      color: var(--brand);
    }

    .tab-counter {
      font-family: var(--font-mono);
      font-size: 0.65625rem;
      padding: 1px 5px;
      border-radius: 10px;
      background-color: var(--surface-2);
      color: var(--muted);
    }

    /* Main Workspace */
    main.studio-content {
      max-width: 1400px;
      margin: 0 auto;
      padding: 24px 28px;
    }

    /* Metrics Bar */
    .metrics-bar {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }

    .metric-card {
      background-color: var(--paper);
      border: 1px solid var(--divider);
      border-radius: 6px;
      padding: 14px 16px;
      display: flex;
      align-items: center;
      gap: 12px;
      box-shadow: var(--shadow-sm);
    }

    .metric-icon-box {
      width: 36px;
      height: 36px;
      border-radius: 4px;
      background-color: var(--surface-2);
      color: var(--brand);
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }

    .metric-value {
      font-size: 1.25rem;
      font-weight: 700;
      color: var(--ink);
      font-family: var(--font-mono);
      line-height: 1.1;
    }

    .metric-label {
      font-size: 0.6875rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      font-weight: 600;
      margin-top: 2px;
    }

    /* Tab Panes */
    .tab-pane {
      display: none;
    }
    .tab-pane.active {
      display: block;
    }

    /* Content Cards */
    .card {
      background-color: var(--paper);
      border: 1px solid var(--divider);
      border-radius: 6px;
      box-shadow: var(--shadow-sm);
      overflow: hidden;
      margin-bottom: 18px;
    }

    .card-header {
      padding: 14px 18px;
      border-bottom: 1px solid var(--divider);
      background-color: var(--surface-2);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }

    .card-header h2, .card-header h3 {
      font-size: 0.875rem;
      font-weight: 700;
      color: var(--ink);
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .card-body {
      padding: 18px;
    }

    /* Section Title */
    .section-title {
      font-size: 0.9375rem;
      font-weight: 700;
      color: var(--ink);
      margin-bottom: 14px;
      display: flex;
      align-items: center;
      gap: 8px;
      letter-spacing: -0.01em;
    }

    /* Detail Grid */
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 14px;
      margin-bottom: 16px;
    }

    .detail-item {
      background-color: var(--surface-2);
      border: 1px solid var(--divider);
      border-radius: 4px;
      padding: 10px 14px;
    }

    .detail-label {
      font-size: 0.6875rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
      margin-bottom: 4px;
    }

    .detail-value {
      font-size: 0.84375rem;
      color: var(--ink);
      font-weight: 600;
    }

    /* Operation Block - Swagger Style */
    .opblock {
      border: 1px solid var(--divider);
      border-radius: 6px;
      background-color: var(--paper);
      margin-bottom: 12px;
      overflow: hidden;
      box-shadow: var(--shadow-sm);
      transition: border-color 0.15s ease;
    }

    .opblock:hover {
      border-color: var(--muted);
    }

    .opblock-header {
      display: flex;
      align-items: center;
      padding: 12px 16px;
      cursor: pointer;
      user-select: none;
      gap: 12px;
      background-color: var(--paper);
      transition: background-color 0.12s ease;
    }

    .opblock-header:hover {
      background-color: var(--surface-2);
    }

    .opblock-badge {
      font-size: 0.6875rem;
      font-weight: 700;
      font-family: var(--font-mono);
      padding: 3px 8px;
      border-radius: 3px;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }

    .badge-agent {
      background-color: var(--badge-agent-bg);
      color: var(--badge-agent-text);
      border: 1px solid var(--badge-agent-border);
    }

    .badge-workflow {
      background-color: var(--badge-wf-bg);
      color: var(--badge-wf-text);
      border: 1px solid var(--badge-wf-border);
    }

    .badge-handler {
      background-color: var(--badge-handler-bg);
      color: var(--badge-handler-text);
      border: 1px solid var(--badge-handler-border);
    }

    .badge-approval {
      background-color: var(--badge-approval-bg);
      color: var(--badge-approval-text);
      border: 1px solid var(--badge-approval-border);
    }

    .badge-join {
      background-color: var(--badge-join-bg);
      color: var(--badge-join-text);
      border: 1px solid var(--badge-join-border);
    }

    .opblock-name {
      font-weight: 700;
      font-size: 0.875rem;
      color: var(--ink);
      font-family: var(--font-mono);
    }

    .opblock-desc {
      font-size: 0.8125rem;
      color: var(--muted);
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .opblock-meta {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 0.75rem;
      color: var(--muted);
    }

    .expand-chevron {
      color: var(--muted);
      display: flex;
      align-items: center;
      transition: transform 0.15s ease;
    }

    .opblock.expanded .expand-chevron {
      transform: rotate(90deg);
    }

    .opblock-body {
      display: none;
      padding: 18px;
      background-color: var(--surface-2);
      border-top: 1px solid var(--divider);
    }

    .opblock.expanded .opblock-body {
      display: block;
    }

    /* Code Block */
    .code-box {
      background-color: var(--code-bg);
      color: var(--code-text);
      border: 1px solid var(--divider);
      border-radius: 4px;
      padding: 12px 16px;
      font-family: var(--font-mono);
      font-size: 0.78125rem;
      line-height: 1.6;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }

    .code-box-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 0.6875rem;
      color: var(--muted);
      font-family: var(--font-sans);
      text-transform: uppercase;
      font-weight: 700;
      letter-spacing: 0.05em;
    }

    /* Tables */
    .custom-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.8125rem;
    }

    .custom-table th {
      text-align: left;
      padding: 9px 12px;
      background-color: var(--surface-2);
      color: var(--muted);
      font-weight: 700;
      border-bottom: 1px solid var(--divider);
      font-size: 0.6875rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .custom-table td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--divider);
      color: var(--ink);
    }

    .custom-table tr:hover td {
      background-color: var(--surface-2);
    }

    /* Pills */
    .pill-list {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 0.6875rem;
      font-family: var(--font-mono);
      padding: 2px 7px;
      border-radius: 3px;
      background-color: var(--paper);
      border: 1px solid var(--divider);
      color: var(--ink);
    }

    .pill-check {
      color: var(--success);
      display: flex;
      align-items: center;
    }

    .pill-cross {
      color: var(--muted);
      display: flex;
      align-items: center;
    }

    /* DAG Graph Section */
    .dag-container {
      background-color: var(--paper);
      border: 1px solid var(--divider);
      border-radius: 6px;
      padding: 20px;
      margin-bottom: 20px;
      overflow-x: auto;
    }

    .dag-stage-row {
      display: flex;
      gap: 28px;
      align-items: flex-start;
      min-width: 600px;
      padding: 8px 0;
    }

    .dag-column {
      display: flex;
      flex-direction: column;
      gap: 14px;
      min-width: 220px;
    }

    .dag-col-header {
      font-size: 0.6875rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      border-bottom: 1px dashed var(--divider);
      padding-bottom: 6px;
      margin-bottom: 4px;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .dag-node-card {
      background-color: var(--paper);
      border: 1px solid var(--divider);
      border-radius: 5px;
      padding: 12px;
      cursor: pointer;
      transition: all 0.15s ease;
      box-shadow: var(--shadow-sm);
    }

    .dag-node-card:hover {
      border-color: var(--brand);
      box-shadow: 0 2px 8px rgba(124, 58, 237, 0.15);
    }

    .dag-node-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 6px;
    }

    .dag-node-id {
      font-family: var(--font-mono);
      font-size: 0.8125rem;
      font-weight: 700;
      color: var(--ink);
    }

    .dag-node-target {
      font-size: 0.75rem;
      color: var(--muted);
      margin-bottom: 8px;
      font-family: var(--font-mono);
    }

    .dag-node-footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 0.6875rem;
      color: var(--muted);
      border-top: 1px solid var(--divider);
      padding-top: 6px;
    }

    /* Modal / Inspector Drawer */
    .drawer-overlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(17, 24, 39, 0.5);
      backdrop-filter: blur(2px);
      z-index: 200;
    }

    .drawer-overlay.active {
      display: block;
    }

    .drawer-panel {
      position: fixed;
      top: 0;
      right: 0;
      bottom: 0;
      width: 520px;
      max-width: 90vw;
      background-color: var(--paper);
      border-left: 1px solid var(--divider);
      box-shadow: var(--shadow-lg);
      padding: 24px;
      overflow-y: auto;
      z-index: 201;
    }

    .drawer-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 20px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--divider);
    }

    .close-drawer-btn {
      background: transparent;
      border: none;
      color: var(--muted);
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 4px;
    }

    .close-drawer-btn:hover {
      color: var(--ink);
    }

    /* Studio Notice Banner */
    .studio-callout {
      border: 1px solid var(--divider);
      border-left: 4px solid var(--brand);
      background-color: var(--paper);
      border-radius: 4px;
      padding: 16px 20px;
      margin-bottom: 20px;
    }

    .studio-callout-header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 6px;
    }

    .studio-callout-header strong {
      font-size: 0.875rem;
      color: var(--ink);
    }

    .studio-callout p {
      font-size: 0.8125rem;
      color: var(--muted);
      line-height: 1.5;
    }

    /* Form Controls */
    .form-group {
      margin-bottom: 14px;
    }

    .form-label {
      display: block;
      font-size: 0.6875rem;
      font-weight: 700;
      color: var(--muted);
      margin-bottom: 5px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .form-control {
      width: 100%;
      background-color: var(--surface-2);
      border: 1px solid var(--divider);
      color: var(--ink);
      padding: 8px 12px;
      border-radius: 4px;
      font-size: 0.8125rem;
      font-family: inherit;
    }

    .form-control:focus {
      outline: 2px solid rgba(124, 58, 237, 0.4);
      border-color: var(--brand);
    }

    textarea.form-control {
      font-family: var(--font-mono);
      font-size: 0.78125rem;
      resize: vertical;
      min-height: 100px;
    }

    /* Toast */
    .toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background-color: #111827;
      color: #ffffff;
      border: 1px solid rgba(255, 255, 255, 0.15);
      padding: 9px 16px;
      border-radius: 4px;
      font-size: 0.8125rem;
      font-weight: 600;
      box-shadow: var(--shadow-lg);
      transform: translateY(80px);
      opacity: 0;
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      z-index: 300;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .toast.show {
      transform: translateY(0);
      opacity: 1;
    }
  </style>
</head>
<body data-theme="dark">

  <!-- Shell Top Bar -->
  <header class="top-bar">
    <div class="studio-brand">
      <div class="studio-brand-logo-badge">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <polygon points="12 2 2 7 12 12 22 7 12 2"/>
          <polyline points="2 17 12 22 22 17"/>
          <polyline points="2 12 12 17 22 12"/>
        </svg>
      </div>
      <div class="studio-brand-text">
        <span class="studio-brand-title">ALGEN</span>
        <span class="studio-brand-subtitle">AGENT RUNTIME</span>
      </div>
      <span class="studio-brand-tagline">specification explorer</span>
    </div>

    <div class="top-bar-actions">
      <div class="search-box">
        <span class="search-icon">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
        </span>
        <input type="text" id="globalSearch" placeholder="Filter agents, nodes, models..." oninput="handleSearch(this.value)">
      </div>

      <button class="btn btn-topbar" onclick="copyRawYaml()" title="Copy agent.yaml contents">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
        <span>Copy YAML</span>
      </button>

      <button class="btn btn-topbar" onclick="downloadStaticHtml()" title="Export standalone static HTML">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/></svg>
        <span>Export HTML</span>
      </button>

      <button class="btn btn-topbar" onclick="toggleTheme()" id="themeToggleBtn" title="Toggle Theme">
        <svg id="themeIcon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>
      </button>
    </div>
  </header>

  <!-- Workspace Subheader -->
  <div class="workspace-header">
    <div>
      <div class="workspace-breadcrumb">runtime / manifest / __PAGE_TITLE__</div>
      <div class="workspace-title-row">
        <h1 class="workspace-title" id="manifestTitle">__PAGE_TITLE__</h1>
        <span class="status-badge" style="background-color: var(--surface-2); color: var(--muted); border: 1px solid var(--divider);">v__PAGE_VERSION__</span>
        <span class="status-badge __LIVE_STATUS_CLASS__">__LIVE_STATUS_TEXT__</span>
      </div>
    </div>

    <div style="display: flex; gap: 8px;">
      <span class="status-badge" style="background-color: var(--surface-2); color: var(--muted); border: 1px solid var(--divider);">
        Runtime v__RUNTIME_VERSION__
      </span>
      <span class="status-badge" style="background-color: var(--surface-2); color: var(--brand); border: 1px solid var(--divider);">
        __EXECUTION_MODE__
      </span>
    </div>
  </div>

  <!-- Studio Product Tabs -->
  <div class="product-tabs-bar">
    <button class="tab-btn active" onclick="switchTab('overview')">
      <span class="tab-step">01</span>
      <span>Overview</span>
    </button>
    <button class="tab-btn" onclick="switchTab('workflows')">
      <span class="tab-step">02</span>
      <span>Workflows (DAG)</span>
      <span class="tab-counter">__STAT_WORKFLOWS__</span>
    </button>
    <button class="tab-btn" onclick="switchTab('agents')">
      <span class="tab-step">03</span>
      <span>Agents Catalog</span>
      <span class="tab-counter">__STAT_AGENTS__</span>
    </button>
    <button class="tab-btn" onclick="switchTab('providers')">
      <span class="tab-step">04</span>
      <span>Providers & Models</span>
      <span class="tab-counter">__STAT_PROVIDERS__</span>
    </button>
    <button class="tab-btn" onclick="switchTab('tools')">
      <span class="tab-step">05</span>
      <span>Tools & MCP</span>
      <span class="tab-counter">__STAT_TOOLS__</span>
    </button>
    <button class="tab-btn" onclick="switchTab('rag')">
      <span class="tab-step">06</span>
      <span>RAG & Knowledge</span>
      <span class="tab-counter">__STAT_RAG__</span>
    </button>
    <button class="tab-btn" onclick="switchTab('execution')">
      <span class="tab-step">07</span>
      <span>Execution (Read-Only)</span>
    </button>
    <button class="tab-btn" onclick="switchTab('yaml')">
      <span class="tab-step">08</span>
      <span>Raw agent.yaml</span>
    </button>
  </div>

  <!-- Main Workspace Content -->
  <main class="studio-content">

    <!-- KPI Metrics Bar -->
    <div class="metrics-bar">
      <div class="metric-card">
        <div class="metric-icon-box">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/></svg>
        </div>
        <div>
          <div class="metric-value">__STAT_AGENTS__</div>
          <div class="metric-label">Agents</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon-box" style="color: var(--brand-cyan);">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 9v12"/><path d="m18 15-6-6"/></svg>
        </div>
        <div>
          <div class="metric-value">__STAT_WORKFLOWS__</div>
          <div class="metric-label">Workflows</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon-box" style="color: #10b981;">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/></svg>
        </div>
        <div>
          <div class="metric-value">__STAT_PROVIDERS__</div>
          <div class="metric-label">Providers</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon-box" style="color: #f59e0b;">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>
        </div>
        <div>
          <div class="metric-value">__STAT_TOOLS__</div>
          <div class="metric-label">Capabilities / Tools</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon-box" style="color: #ec4899;">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/></svg>
        </div>
        <div>
          <div class="metric-value">__STAT_RAG__</div>
          <div class="metric-label">RAG Retrieval Stores</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon-box" style="color: var(--muted);">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" x2="2" y1="12" y2="12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" x2="6.01" y1="16" y2="16"/><line x1="10" x2="10.01" y1="16" y2="16"/></svg>
        </div>
        <div>
          <div class="metric-value" style="font-size: 1rem;">__STAT_STORAGE__</div>
          <div class="metric-label">Persistence Engine</div>
        </div>
      </div>
    </div>

    <!-- TAB 1: OVERVIEW -->
    <div id="pane-overview" class="tab-pane active">
      <div class="card">
        <div class="card-header">
          <h2>Manifest Specification Summary</h2>
        </div>
        <div class="card-body">
          <p style="color: var(--muted); font-size: 0.875rem; margin-bottom: 16px;">
            __PAGE_DESC__
          </p>

          <div class="detail-grid">
            <div class="detail-item">
              <div class="detail-label">Runtime Engine</div>
              <div class="detail-value">Algen Agent Runtime v__RUNTIME_VERSION__</div>
            </div>
            <div class="detail-item">
              <div class="detail-label">Execution Architecture</div>
              <div class="detail-value">__EXECUTION_MODE__</div>
            </div>
            <div class="detail-item">
              <div class="detail-label">Credential Hygiene</div>
              <div class="detail-value" style="color: var(--success); display: flex; align-items: center; gap: 5px;">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                <span>Secret references verified (env://)</span>
              </div>
            </div>
            <div class="detail-item">
              <div class="detail-label">Canonical REST Endpoint</div>
              <div class="detail-value" style="font-family: var(--font-mono); font-size: 0.8125rem;">__API_ENDPOINT__</div>
            </div>
          </div>
        </div>
      </div>

      <div class="section-title">Architecture Components</div>
      <div class="detail-grid" style="grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));">
        <div class="card">
          <div class="card-header">
            <h3>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/></svg>
              <span>Agents</span>
            </h3>
          </div>
          <div class="card-body" style="padding: 0 16px;">
            <ul style="list-style: none; padding: 0;" id="overviewAgentsList"></ul>
          </div>
        </div>

        <div class="card">
          <div class="card-header">
            <h3>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 9v12"/><path d="m18 15-6-6"/></svg>
              <span>Workflows</span>
            </h3>
          </div>
          <div class="card-body" style="padding: 0 16px;">
            <ul style="list-style: none; padding: 0;" id="overviewWorkflowsList"></ul>
          </div>
        </div>

        <div class="card">
          <div class="card-header">
            <h3>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/></svg>
              <span>Providers</span>
            </h3>
          </div>
          <div class="card-body" style="padding: 0 16px;">
            <ul style="list-style: none; padding: 0;" id="overviewProvidersList"></ul>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 2: WORKFLOWS (DAG) -->
    <div id="pane-workflows" class="tab-pane">
      <div id="workflowsContainer"></div>
    </div>

    <!-- TAB 3: AGENTS CATALOG -->
    <div id="pane-agents" class="tab-pane">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
        <div class="section-title" style="margin-bottom: 0;">Agents Specification</div>
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-secondary" onclick="toggleAllAccordions('agent', true)">Expand All</button>
          <button class="btn btn-secondary" onclick="toggleAllAccordions('agent', false)">Collapse All</button>
        </div>
      </div>
      <div id="agentsCatalog"></div>
    </div>

    <!-- TAB 4: PROVIDERS & MODELS -->
    <div id="pane-providers" class="tab-pane">
      <div class="section-title">Configured Model Providers</div>
      <div id="providersCatalog"></div>
    </div>

    <!-- TAB 5: TOOLS & MCP -->
    <div id="pane-tools" class="tab-pane">
      <div class="section-title">Tools & MCP Servers</div>
      <div id="toolsCatalog"></div>
    </div>

    <!-- TAB 6: RAG & KNOWLEDGE -->
    <div id="pane-rag" class="tab-pane">
      <div class="section-title">Retrieval & Knowledge Sources</div>
      <div id="ragCatalog"></div>
    </div>

    <!-- TAB 7: EXECUTION (READ-ONLY) -->
    <div id="pane-execution" class="tab-pane">
      <!-- Studio Integration Callout -->
      <div class="studio-callout">
        <div class="studio-callout-header">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--brand)" stroke-width="2.5"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>
          <strong>Interactive Execution is Hosted in Algen Agent Studio</strong>
          <span class="status-badge" style="background-color: var(--brand-pale); color: var(--brand); border: 1px solid var(--brand);">Read-Only Specification</span>
        </div>
        <p>
          This built-in Runtime Explorer is a declarative, read-only specification view for <code>agent.yaml</code>. 
          To interactively prompt agents, view live streaming tokens, inspect distributed trace spans, fork configurations, or run automated evaluation gates, launch <strong>Algen Agent Studio</strong> (<code>algen-agent-studio</code>) or import this directory.
        </p>
      </div>

      <div class="card">
        <div class="card-header">
          <h2>REST API & SDK Execution Contracts</h2>
        </div>
        <div class="card-body">
          <p style="color: var(--muted); font-size: 0.8125rem; margin-bottom: 16px;">
            Agents and workflows declared in this specification are exposed through Runtime's production API. Use the parameters below to generate copyable invocation contracts.
          </p>

          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
            <div>
              <div class="form-group">
                <label class="form-label" for="runTargetSelect">Target Agent</label>
                <select id="runTargetSelect" class="form-control" onchange="updateExecutionSnippets()">
                  <!-- populated by JS -->
                </select>
              </div>

              <div class="form-group">
                <label class="form-label" for="runPromptInput">Input Prompt Payload</label>
                <textarea id="runPromptInput" class="form-control" oninput="updateExecutionSnippets()">Hello, summarize what you can do.</textarea>
              </div>

              <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                <div class="form-group">
                  <label class="form-label" for="runTenantInput">Tenant ID Header</label>
                  <input type="text" id="runTenantInput" class="form-control" value="default-tenant" oninput="updateExecutionSnippets()">
                </div>
                <div class="form-group">
                  <label class="form-label" for="runUserInput">User ID Header</label>
                  <input type="text" id="runUserInput" class="form-control" value="default-user" oninput="updateExecutionSnippets()">
                </div>
              </div>
            </div>

            <div>
              <div class="code-box-header">
                <span>cURL Invocation</span>
                <button class="btn btn-secondary" style="height: 24px; padding: 0 8px; font-size: 0.6875rem;" onclick="copyCurlSnippet()">Copy cURL</button>
              </div>
              <div class="code-box" id="curlSnippetBox" style="margin-bottom: 14px; max-height: 180px;"></div>

              <div class="code-box-header">
                <span>Python SDK Invocation</span>
                <button class="btn btn-secondary" style="height: 24px; padding: 0 8px; font-size: 0.6875rem;" onclick="copyPythonSnippet()">Copy Python</button>
              </div>
              <div class="code-box" id="pythonSnippetBox" style="max-height: 180px;"></div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 8: RAW YAML -->
    <div id="pane-yaml" class="tab-pane">
      <div class="card">
        <div class="card-header">
          <h2>agent.yaml Source</h2>
          <button class="btn btn-secondary" onclick="copyRawYaml()">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
            <span>Copy Source</span>
          </button>
        </div>
        <div class="card-body" style="padding: 0;">
          <div class="code-box" id="rawYamlBox" style="max-height: 700px; border: none; border-radius: 0;"></div>
        </div>
      </div>
    </div>

  </main>

  <!-- Slide-Over Drawer for Node Inspection -->
  <div class="drawer-overlay" id="nodeDrawerOverlay" onclick="closeNodeDrawer()"></div>
  <div class="drawer-panel" id="nodeDrawerPanel" style="display: none;">
    <div class="drawer-header">
      <div style="display: flex; align-items: center; gap: 8px;">
        <span class="opblock-badge badge-workflow" id="drawerKindBadge">AGENT</span>
        <h3 id="drawerNodeId" style="font-family: var(--font-mono); font-size: 1rem; font-weight: 700;">node-id</h3>
      </div>
      <button class="close-drawer-btn" onclick="closeNodeDrawer()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" x2="6" y1="6" y2="18"/><line x1="6" x2="18" y1="6" y2="18"/></svg>
      </button>
    </div>
    <div id="drawerBody"></div>
  </div>

  <!-- Toast Notification -->
  <div class="toast" id="toast">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
    <span id="toastMsg">Copied to clipboard</span>
  </div>

  <script>
    const SPEC = __SPEC_JSON__;

    function init() {
      renderOverview();
      renderWorkflows();
      renderAgents();
      renderProviders();
      renderTools();
      renderRag();
      renderRawYaml();
      populateExecutionTargets();
      updateExecutionSnippets();
    }

    function switchTab(tabId) {
      document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(pane => pane.classList.remove('active'));

      const targetPane = document.getElementById('pane-' + tabId);
      if (targetPane) targetPane.classList.add('active');

      const activeBtn = Array.from(document.querySelectorAll('.tab-btn')).find(b => 
        b.getAttribute('onclick') && b.getAttribute('onclick').includes(tabId)
      );
      if (activeBtn) activeBtn.classList.add('active');
    }

    function toggleTheme() {
      const current = document.body.getAttribute('data-theme');
      const next = current === 'dark' ? 'light' : 'dark';
      document.body.setAttribute('data-theme', next);
    }

    function showToast(message) {
      const toast = document.getElementById('toast');
      document.getElementById('toastMsg').innerText = message;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 2200);
    }

    function copyRawYaml() {
      navigator.clipboard.writeText(SPEC.raw_yaml || '').then(() => {
        showToast('agent.yaml copied to clipboard');
      }).catch(() => {
        showToast('Failed to copy');
      });
    }

    function downloadStaticHtml() {
      const blob = new Blob([document.documentElement.outerHTML], { type: 'text/html' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = (SPEC.title || 'agent-ui').toLowerCase().replace(/[^a-z0-9]/g, '-') + '.html';
      a.click();
      URL.revokeObjectURL(url);
    }

    function renderOverview() {
      const agentsList = document.getElementById('overviewAgentsList');
      if (SPEC.agents && SPEC.agents.length > 0) {
        agentsList.innerHTML = SPEC.agents.map(a => `
          <li style="padding: 8px 0; border-bottom: 1px solid var(--divider); display: flex; justify-content: space-between; align-items: center;">
            <span style="font-family: var(--font-mono); font-weight: 600; font-size: 0.8125rem;">${escapeHtml(a.name)}</span>
            <span class="status-badge" style="background-color: var(--surface-2); color: var(--muted); border: 1px solid var(--divider);">${escapeHtml(a.default_model?.model || 'default')}</span>
          </li>
        `).join('');
      } else {
        agentsList.innerHTML = '<li style="color: var(--muted); font-size: 0.8125rem; padding: 12px 0;">No agents declared.</li>';
      }

      const workflowsList = document.getElementById('overviewWorkflowsList');
      const wfKeys = Object.keys(SPEC.workflows || {});
      if (wfKeys.length > 0) {
        workflowsList.innerHTML = wfKeys.map(k => {
          const wf = SPEC.workflows[k];
          return `
            <li style="padding: 8px 0; border-bottom: 1px solid var(--divider); display: flex; justify-content: space-between; align-items: center;">
              <span style="font-family: var(--font-mono); font-weight: 600; font-size: 0.8125rem;">${escapeHtml(wf.name)}</span>
              <span class="status-badge" style="background-color: var(--surface-2); color: var(--brand); border: 1px solid var(--divider);">${wf.nodes ? wf.nodes.length : 0} nodes</span>
            </li>
          `;
        }).join('');
      } else {
        workflowsList.innerHTML = '<li style="color: var(--muted); font-size: 0.8125rem; padding: 12px 0;">Autonomous single-agent mode.</li>';
      }

      const providersList = document.getElementById('overviewProvidersList');
      if (SPEC.providers && SPEC.providers.length > 0) {
        providersList.innerHTML = SPEC.providers.map(p => `
          <li style="padding: 8px 0; border-bottom: 1px solid var(--divider); display: flex; justify-content: space-between; align-items: center;">
            <span style="font-family: var(--font-mono); font-weight: 600; font-size: 0.8125rem;">${escapeHtml(p.id)} (${escapeHtml(p.type)})</span>
            <span class="status-badge" style="background-color: var(--surface-2); color: var(--muted); border: 1px solid var(--divider);">${escapeHtml(p.default_model)}</span>
          </li>
        `).join('');
      } else {
        providersList.innerHTML = '<li style="color: var(--muted); font-size: 0.8125rem; padding: 12px 0;">Deterministic mock provider.</li>';
      }
    }

    function renderWorkflows() {
      const container = document.getElementById('workflowsContainer');
      const wfKeys = Object.keys(SPEC.workflows || {});
      if (wfKeys.length === 0) {
        container.innerHTML = `
          <div class="card" style="padding: 40px 24px; text-align: center; color: var(--muted);">
            <div style="font-size: 1rem; font-weight: 700; color: var(--ink); margin-bottom: 6px;">Single-Agent Project</div>
            <p style="font-size: 0.8125rem; max-width: 500px; margin: 0 auto;">No multi-agent workflow DAG is declared in this specification. Declare a <code>workflows:</code> mapping in <code>agent.yaml</code> to view the visual DAG layout.</p>
          </div>
        `;
        return;
      }

      container.innerHTML = wfKeys.map(key => {
        const wf = SPEC.workflows[key];
        const dag = wf.dag || { columns: [] };

        const columnsHtml = dag.columns.map((col, colIdx) => `
          <div class="dag-column">
            <div class="dag-col-header">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
              <span>Stage ${colIdx + 1}</span>
            </div>
            ${col.map(nodeId => {
              const node = wf.nodes.find(n => n.id === nodeId);
              if (!node) return '';
              const badgeClass = getNodeBadgeClass(node.kind);
              const target = node.agent || node.handler || node.workflow_name || node.kind;
              return `
                <div class="dag-node-card" onclick="openNodeInspector('${key}', '${node.id}')">
                  <div class="dag-node-header">
                    <span class="dag-node-id">${escapeHtml(node.id)}</span>
                    <span class="opblock-badge ${badgeClass}">${escapeHtml(node.kind)}</span>
                  </div>
                  <div class="dag-node-target">&rarr; ${escapeHtml(target)}</div>
                  <div class="dag-node-footer">
                    <span>${node.depends_on && node.depends_on.length > 0 ? 'dep: ' + node.depends_on.join(', ') : 'root'}</span>
                    <span>key: ${escapeHtml(node.output_key)}</span>
                  </div>
                </div>
              `;
            }).join('')}
          </div>
        `).join('');

        return `
          <div class="card" style="margin-bottom: 24px;">
            <div class="card-header">
              <div>
                <h3 style="font-family: var(--font-mono); font-size: 0.9375rem;">${escapeHtml(wf.name)}</h3>
                <div style="font-size: 0.75rem; color: var(--muted); margin-top: 2px;">${escapeHtml(wf.description || 'Multi-agent workflow topology')}</div>
              </div>
              <div style="display: flex; gap: 8px;">
                <span class="status-badge" style="background-color: var(--surface-2); color: var(--muted); border: 1px solid var(--divider);">concurrency: ${wf.maximum_concurrency}</span>
                <span class="status-badge" style="background-color: var(--surface-2); color: var(--muted); border: 1px solid var(--divider);">timeout: ${wf.timeout_seconds}s</span>
              </div>
            </div>

            <div class="dag-container" style="border: none; border-radius: 0; margin-bottom: 0;">
              <div class="dag-stage-row">
                ${columnsHtml}
              </div>
            </div>
          </div>
        `;
      }).join('');
    }

    function getNodeBadgeClass(kind) {
      switch(kind) {
        case 'agent': return 'badge-agent';
        case 'map_agent': return 'badge-workflow';
        case 'handler': return 'badge-handler';
        case 'approval': return 'badge-approval';
        case 'join': return 'badge-join';
        default: return 'badge-agent';
      }
    }

    function openNodeInspector(workflowKey, nodeId) {
      const wf = SPEC.workflows[workflowKey];
      if (!wf) return;
      const node = wf.nodes.find(n => n.id === nodeId);
      if (!node) return;

      document.getElementById('drawerNodeId').innerText = node.id;
      const badge = document.getElementById('drawerKindBadge');
      badge.innerText = node.kind.toUpperCase();
      badge.className = 'opblock-badge ' + getNodeBadgeClass(node.kind);

      const body = document.getElementById('drawerBody');
      body.innerHTML = `
        <div class="detail-item" style="margin-bottom: 12px;">
          <div class="detail-label">Execution Target</div>
          <div class="detail-value" style="font-family: var(--font-mono); font-size: 0.8125rem;">${escapeHtml(node.agent || node.handler || node.workflow_name || 'N/A')}</div>
        </div>

        <div class="detail-grid" style="grid-template-columns: 1fr 1fr; margin-bottom: 12px;">
          <div class="detail-item">
            <div class="detail-label">Dependencies</div>
            <div class="detail-value" style="font-family: var(--font-mono); font-size: 0.8125rem;">${node.depends_on.length > 0 ? node.depends_on.join(', ') : '(None - Root Node)'}</div>
          </div>
          <div class="detail-item">
            <div class="detail-label">Output Key</div>
            <div class="detail-value" style="font-family: var(--font-mono); font-size: 0.8125rem;">${escapeHtml(node.output_key)}</div>
          </div>
        </div>

        ${node.input_builder || node.input_template ? `
          <div class="detail-item" style="margin-bottom: 12px;">
            <div class="detail-label">Input Specification</div>
            <div class="detail-value" style="font-family: var(--font-mono); font-size: 0.8125rem;">
              ${node.input_builder ? 'Builder: ' + escapeHtml(node.input_builder) : 'Template: ' + escapeHtml(node.input_template)}
            </div>
          </div>
        ` : ''}

        ${node.approval ? `
          <div class="card" style="padding: 14px; margin-bottom: 12px; border-left: 3px solid var(--warning);">
            <div class="detail-label" style="color: var(--warning); display: flex; align-items: center; gap: 5px;">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/></svg>
              <span>Human Approval Gate</span>
            </div>
            <div style="font-size: 0.8125rem; margin: 6px 0;"><strong>Prompt:</strong> ${escapeHtml(node.approval.prompt)}</div>
            <div style="font-size: 0.75rem; color: var(--muted); font-family: var(--font-mono);">
              Review From: ${node.approval.review_from ? node.approval.review_from.join(', ') : 'all'} | 
              Allow Modification: ${node.approval.allow_modification ? 'true' : 'false'} | 
              Timeout: ${node.approval.expires_seconds}s
            </div>
          </div>
        ` : ''}

        ${node.pause ? `
          <div class="card" style="padding: 14px; margin-bottom: 12px; border-left: 3px solid var(--brand-cyan);">
            <div class="detail-label" style="color: var(--brand-cyan);">Clarification Pause Rule</div>
            <div style="font-size: 0.8125rem; margin: 4px 0; font-family: var(--font-mono);">Condition: ${escapeHtml(JSON.stringify(node.pause.when))}</div>
            <div style="font-size: 0.75rem; color: var(--muted);">Max Occurrences: ${node.pause.maximum_occurrences}</div>
          </div>
        ` : ''}

        ${node.resources && node.resources.length > 0 ? `
          <div style="margin-bottom: 14px;">
            <div class="detail-label" style="margin-bottom: 6px;">Declared Resources</div>
            <div class="pill-list">
              ${node.resources.map(r => `
                <span class="pill">
                  <strong>${escapeHtml(r.kind)}:</strong> ${escapeHtml(r.name)} (${escapeHtml(r.access)})
                </span>
              `).join('')}
            </div>
          </div>
        ` : ''}

        <div class="detail-grid" style="grid-template-columns: 1fr 1fr; margin-bottom: 12px;">
          <div class="detail-item">
            <div class="detail-label">Recovery Policy</div>
            <div class="detail-value" style="font-family: var(--font-mono); font-size: 0.8125rem;">${escapeHtml(node.recovery_policy)}</div>
          </div>
          <div class="detail-item">
            <div class="detail-label">Failure Policy</div>
            <div class="detail-value" style="font-family: var(--font-mono); font-size: 0.8125rem;">${escapeHtml(node.failure_policy)}</div>
          </div>
        </div>

        ${node.output_schema ? `
          <div class="form-group">
            <div class="detail-label">Output Schema (JSON Schema)</div>
            <div class="code-box" style="max-height: 200px;">${escapeHtml(JSON.stringify(node.output_schema, null, 2))}</div>
          </div>
        ` : ''}
      `;

      document.getElementById('nodeDrawerOverlay').classList.add('active');
      document.getElementById('nodeDrawerPanel').style.display = 'block';
    }

    function closeNodeDrawer() {
      document.getElementById('nodeDrawerOverlay').classList.remove('active');
      document.getElementById('nodeDrawerPanel').style.display = 'none';
    }

    function renderAgents() {
      const container = document.getElementById('agentsCatalog');
      if (!SPEC.agents || SPEC.agents.length === 0) {
        container.innerHTML = '<div class="card" style="padding: 32px; text-align: center; color: var(--muted);">No agents declared.</div>';
        return;
      }

      container.innerHTML = SPEC.agents.map((ag, idx) => `
        <div class="opblock ${idx === 0 ? 'expanded' : ''}" data-name="${escapeHtml(ag.name.toLowerCase())}">
          <div class="opblock-header" onclick="toggleOpblock(this.parentElement)">
            <span class="opblock-badge badge-agent">AGENT</span>
            <span class="opblock-name">${escapeHtml(ag.name)}</span>
            <span class="status-badge" style="background-color: var(--surface-2); color: var(--muted); border: 1px solid var(--divider);">v${escapeHtml(ag.version)}</span>
            <span class="opblock-desc">${escapeHtml(ag.description || '')}</span>
            <div class="opblock-meta">
              <span class="pill">${escapeHtml(ag.default_model?.model || 'model')}</span>
              <span class="pill">${escapeHtml(ag.planning_strategy)}</span>
              <span class="expand-chevron">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
              </span>
            </div>
          </div>

          <div class="opblock-body">
            <div class="detail-grid">
              <div class="detail-item">
                <div class="detail-label">Model Profile</div>
                <div class="detail-value" style="font-family: var(--font-mono); font-size: 0.8125rem;">
                  ${escapeHtml(ag.default_model?.provider || 'default')} / ${escapeHtml(ag.default_model?.model || 'default')}
                </div>
                ${ag.default_model?.required_capabilities && ag.default_model.required_capabilities.length > 0 ? `
                  <div style="margin-top: 6px;" class="pill-list">
                    ${ag.default_model.required_capabilities.map(c => `<span class="pill">${escapeHtml(c)}</span>`).join('')}
                  </div>
                ` : ''}
              </div>

              <div class="detail-item">
                <div class="detail-label">Planning & Execution</div>
                <div class="detail-value" style="font-size: 0.8125rem;">
                  Strategy: <strong>${escapeHtml(ag.planning_strategy)}</strong> | Max Steps: <strong>${ag.max_steps}</strong>
                </div>
                <div style="font-size: 0.75rem; color: var(--muted); margin-top: 4px;">
                  Context: ${escapeHtml(ag.context_builder)} | Composer: ${escapeHtml(ag.response_composer)}
                </div>
              </div>

              <div class="detail-item">
                <div class="detail-label">Assigned Capabilities & Tools</div>
                <div class="detail-value">
                  ${ag.enabled_tools && ag.enabled_tools.length > 0 ? `
                    <div class="pill-list">
                      ${ag.enabled_tools.map(t => `<span class="pill" style="color: var(--success); font-weight: 600;">${escapeHtml(t)}</span>`).join('')}
                    </div>
                  ` : '<span style="color: var(--muted); font-size: 0.75rem;">None assigned</span>'}
                </div>
              </div>
            </div>

            <div style="margin-bottom: 14px;">
              <div class="code-box-header">
                <span>System Instructions</span>
                <button class="btn btn-secondary" style="height: 22px; padding: 0 8px; font-size: 0.6875rem;" onclick="navigator.clipboard.writeText('${escapeJsString(ag.system_instructions)}').then(() => showToast('Instructions copied'))">Copy</button>
              </div>
              <div class="code-box" style="max-height: 200px;">${escapeHtml(ag.system_instructions)}</div>
            </div>

            <div class="card" style="padding: 12px; margin-bottom: 0; background-color: var(--paper);">
              <div class="detail-label" style="margin-bottom: 8px;">Governance & Operational Policies</div>
              <div class="detail-grid" style="grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); margin-bottom: 0;">
                <div>
                  <div style="font-size: 0.6875rem; color: var(--muted); font-weight: 700;">MEMORY POLICY</div>
                  <div style="font-size: 0.78125rem; font-family: var(--font-mono);">${escapeHtml(ag.memory_policy?.scope || 'tenant')} (max: ${ag.memory_policy?.maximum_messages || 'default'})</div>
                </div>
                <div>
                  <div style="font-size: 0.6875rem; color: var(--muted); font-weight: 700;">GUARDRAIL POLICY</div>
                  <div style="font-size: 0.78125rem; font-family: var(--font-mono);">PII: ${ag.guardrail_policy?.detect_pii ? 'Active' : 'None'} | Injection: ${ag.guardrail_policy?.detect_prompt_injection ? 'Active' : 'None'}</div>
                </div>
                <div>
                  <div style="font-size: 0.6875rem; color: var(--muted); font-weight: 700;">VERIFICATION</div>
                  <div style="font-size: 0.78125rem; font-family: var(--font-mono);">${ag.verification_policy?.strict_json_schema ? 'Strict Schema' : 'Standard'}</div>
                </div>
                <div>
                  <div style="font-size: 0.6875rem; color: var(--muted); font-weight: 700;">RETRY POLICY</div>
                  <div style="font-size: 0.78125rem; font-family: var(--font-mono);">Max Attempts: ${ag.retry_policy?.max_attempts || 1}</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      `).join('');
    }

    function toggleOpblock(el) {
      el.classList.toggle('expanded');
    }

    function toggleAllAccordions(prefix, expand) {
      document.querySelectorAll('.opblock').forEach(b => {
        if (expand) b.classList.add('expanded');
        else b.classList.remove('expanded');
      });
    }

    function renderProviders() {
      const container = document.getElementById('providersCatalog');
      if (!SPEC.providers || SPEC.providers.length === 0) {
        container.innerHTML = '<div class="card" style="padding: 24px; color: var(--muted);">No external providers declared. Deterministic mock provider active.</div>';
        return;
      }

      container.innerHTML = `
        <table class="custom-table card">
          <thead>
            <tr>
              <th>Provider Instance ID</th>
              <th>Adapter Type</th>
              <th>Default Model</th>
              <th>Endpoint</th>
              <th>Capabilities</th>
              <th>Secret Reference</th>
            </tr>
          </thead>
          <tbody>
            ${SPEC.providers.map(p => `
              <tr>
                <td style="font-family: var(--font-mono); font-weight: 700;">${escapeHtml(p.id)}</td>
                <td><span class="opblock-badge badge-handler">${escapeHtml(p.type)}</span></td>
                <td style="font-family: var(--font-mono); font-size: 0.8125rem;">${escapeHtml(p.default_model)}</td>
                <td style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--muted);">${escapeHtml(p.base_url || 'Default SDK Endpoint')}</td>
                <td>
                  <div class="pill-list">
                    ${renderCapabilityPills(p.capabilities)}
                  </div>
                </td>
                <td>
                  ${p.api_key_ref ? `
                    <span class="pill" style="border-color: rgba(36, 116, 81, 0.4); color: var(--success);">
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                      <span>${escapeHtml(p.api_key_ref)}</span>
                    </span>
                  ` : '<span class="pill" style="color: var(--muted);">None required</span>'}
                </td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      `;
    }

    function renderCapabilityPills(caps) {
      if (!caps) return '<span style="color: var(--muted);">-</span>';
      const items = ['chat', 'streaming', 'tools', 'structured_output', 'embeddings', 'images'];
      return items.map(k => {
        const ok = caps[k];
        return `
          <span class="pill">
            <span class="${ok ? 'pill-check' : 'pill-cross'}">
              ${ok ? '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>' : '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><line x1="18" x2="6" y1="6" y2="18"/><line x1="6" x2="18" y1="6" y2="18"/></svg>'}
            </span>
            <span>${k}</span>
          </span>
        `;
      }).join('');
    }

    function renderTools() {
      const container = document.getElementById('toolsCatalog');
      const tools = SPEC.tools || [];
      const mcp = SPEC.mcp_servers || [];

      let html = '';

      if (mcp.length > 0) {
        html += `
          <h3 style="font-size: 0.875rem; font-weight: 700; margin-bottom: 10px;">Model Context Protocol (MCP) Servers</h3>
          <div class="detail-grid" style="margin-bottom: 24px;">
            ${mcp.map(s => `
              <div class="card" style="padding: 14px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                  <strong style="font-family: var(--font-mono); font-size: 0.8125rem;">${escapeHtml(s.name)}</strong>
                  <span class="opblock-badge badge-workflow">${escapeHtml(s.transport)}</span>
                </div>
                <div style="font-size: 0.75rem; font-family: var(--font-mono); color: var(--muted); margin-bottom: 6px;">
                  ${s.command ? 'Command: ' + escapeHtml(s.command) + ' ' + (s.args || []).join(' ') : 'URL: ' + escapeHtml(s.url)}
                </div>
                ${s.env_keys && s.env_keys.length > 0 ? `
                  <div style="font-size: 0.6875rem; color: var(--muted);">Environment variables: ${s.env_keys.join(', ')}</div>
                ` : ''}
              </div>
            `).join('')}
          </div>
        `;
      }

      html += `
        <h3 style="font-size: 0.875rem; font-weight: 700; margin-bottom: 10px;">Active Capabilities & Tools</h3>
      `;

      if (tools.length === 0) {
        html += '<div class="card" style="padding: 24px; color: var(--muted);">No custom tools declared in this specification.</div>';
      } else {
        html += `
          <table class="custom-table card">
            <thead>
              <tr>
                <th>Capability / Tool Name</th>
                <th>Assigned Agents</th>
                <th>Referencing Workflow Nodes</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              ${tools.map(t => `
                <tr>
                  <td style="font-family: var(--font-mono); font-weight: 600; color: var(--brand);">${escapeHtml(t.name)}</td>
                  <td>${t.used_by_agents && t.used_by_agents.length > 0 ? t.used_by_agents.map(a => `<span class="pill">${escapeHtml(a)}</span>`).join(' ') : '<span style="color:var(--muted);">-</span>'}</td>
                  <td>${t.used_by_nodes && t.used_by_nodes.length > 0 ? t.used_by_nodes.map(n => `<span class="pill">${escapeHtml(n)}</span>`).join(' ') : '<span style="color:var(--muted);">-</span>'}</td>
                  <td style="color: var(--muted); font-size: 0.78125rem;">${escapeHtml(t.description || 'Declared runtime tool capability')}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
      }

      container.innerHTML = html;
    }

    function renderRag() {
      const container = document.getElementById('ragCatalog');
      const retrieval = SPEC.retrieval || [];
      if (retrieval.length === 0) {
        container.innerHTML = '<div class="card" style="padding: 24px; color: var(--muted);">No retrieval or RAG stores configured.</div>';
        return;
      }

      container.innerHTML = `
        <div class="detail-grid">
          ${retrieval.map(r => `
            <div class="card" style="padding: 16px;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                <strong style="font-size: 0.9375rem; font-family: var(--font-mono);">${escapeHtml(r.name)}</strong>
                <span class="opblock-badge badge-handler">${escapeHtml(r.type)}</span>
              </div>
              <div class="detail-grid" style="grid-template-columns: 1fr 1fr; margin-bottom: 8px;">
                <div class="detail-item">
                  <div class="detail-label">Search Mode</div>
                  <div class="detail-value" style="font-size: 0.8125rem;">${escapeHtml(r.mode)}</div>
                </div>
                <div class="detail-item">
                  <div class="detail-label">Chunking</div>
                  <div class="detail-value" style="font-size: 0.8125rem;">${r.chunk_size} (overlap: ${r.chunk_overlap})</div>
                </div>
              </div>
              <div style="font-size: 0.75rem; color: var(--muted); font-family: var(--font-mono);">
                Embedding Provider: ${escapeHtml(r.embedding_provider || 'default')} / ${escapeHtml(r.embedding_model || 'default')} (${r.embedding_dimensions}d)
              </div>
            </div>
          `).join('')}
        </div>
      `;
    }

    function renderRawYaml() {
      document.getElementById('rawYamlBox').innerText = SPEC.raw_yaml || '';
    }

    function populateExecutionTargets() {
      const select = document.getElementById('runTargetSelect');
      if (!SPEC.agents || SPEC.agents.length === 0) {
        select.innerHTML = '<option value="">No agents available</option>';
        return;
      }
      select.innerHTML = SPEC.agents.map(a => `
        <option value="${escapeHtml(a.name)}">${escapeHtml(a.name)} (v${escapeHtml(a.version)})</option>
      `).join('');
    }

    function updateExecutionSnippets() {
      const agent = document.getElementById('runTargetSelect')?.value || 'minimal';
      const prompt = document.getElementById('runPromptInput')?.value || 'Hello';
      const tenant = document.getElementById('runTenantInput')?.value || 'default-tenant';
      const user = document.getElementById('runUserInput')?.value || 'default-user';
      const endpoint = SPEC.api_endpoint || 'http://127.0.0.1:8000/v1/runs';

      const curlText = `curl -X POST "${endpoint}" \\
  -H "Content-Type: application/json" \\
  -H "X-Tenant-ID: ${tenant}" \\
  -H "X-User-ID: ${user}" \\
  -H "X-Scopes: runs:read runs:write" \\
  -d '${JSON.stringify({ agent: agent, input: prompt }, null, 2)}'`;

      const pythonText = `from algen_agent_runtime import AlgenAgentRuntimeClient
from algen_agent_runtime.types.contracts import RunRequest

client = AlgenAgentRuntimeClient(container.runtime)
result = await client.run(
    RunRequest(
        agent="${agent}",
        input="${prompt.replace(/\\n/g, ' ')}",
        tenant_id="${tenant}",
        user_id="${user}",
    )
)
print(result.output)`;

      const curlBox = document.getElementById('curlSnippetBox');
      const pyBox = document.getElementById('pythonSnippetBox');
      if (curlBox) curlBox.innerText = curlText;
      if (pyBox) pyBox.innerText = pythonText;
    }

    function copyCurlSnippet() {
      const text = document.getElementById('curlSnippetBox')?.innerText || '';
      navigator.clipboard.writeText(text).then(() => showToast('cURL command copied'));
    }

    function copyPythonSnippet() {
      const text = document.getElementById('pythonSnippetBox')?.innerText || '';
      navigator.clipboard.writeText(text).then(() => showToast('Python snippet copied'));
    }

    function handleSearch(query) {
      const q = query.trim().toLowerCase();
      document.querySelectorAll('.opblock').forEach(block => {
        const name = block.getAttribute('data-name') || '';
        const text = block.innerText.toLowerCase();
        if (!q || name.includes(q) || text.includes(q)) {
          block.style.display = 'block';
        } else {
          block.style.display = 'none';
        }
      });
    }

    function escapeHtml(str) {
      if (str === null || str === undefined) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    function escapeJsString(str) {
      if (!str) return '';
      return str.replace(/\\\\/g, '\\\\\\\\').replace(/'/g, "\\\\'").replace(/\\n/g, '\\\\n');
    }

    document.addEventListener('DOMContentLoaded', init);
  </script>
</body>
</html>"""


def render_agent_ui(spec: dict[str, Any]) -> str:
    """Render a fully self-contained, standalone HTML document for the Agent Manifest UI."""
    spec_json = json.dumps(spec).replace("</script>", "<\\/script>")

    stats = spec.get("stats", {})
    live_server = bool(spec.get("live_server", False))

    output = HTML_TEMPLATE
    output = output.replace("__SPEC_JSON__", spec_json)
    output = output.replace("__PAGE_TITLE__", str(spec.get("title", "Algen Agent UI")))
    output = output.replace("__PAGE_VERSION__", str(spec.get("version", "1.0.0")))
    output = output.replace(
        "__PAGE_DESC__",
        str(spec.get("description") or "Declared Algen Agent Runtime project specification."),
    )
    output = output.replace("__RUNTIME_VERSION__", str(spec.get("runtime_version", "1.0.0")))
    output = output.replace(
        "__LIVE_STATUS_CLASS__", "status-badge-live" if live_server else "status-badge-static"
    )
    output = output.replace(
        "__LIVE_STATUS_TEXT__", "LIVE ENDPOINT" if live_server else "STATIC MANIFEST"
    )
    output = output.replace(
        "__EXECUTION_MODE__",
        "Workflow DAG" if spec.get("workflows") else "Single-Agent",
    )
    output = output.replace("__API_ENDPOINT__", str(spec.get("api_endpoint", "/v1/runs")))

    output = output.replace("__STAT_AGENTS__", str(stats.get("agent_count", 0)))
    output = output.replace("__STAT_WORKFLOWS__", str(stats.get("workflow_count", 0)))
    output = output.replace("__STAT_PROVIDERS__", str(stats.get("provider_count", 0)))
    output = output.replace("__STAT_TOOLS__", str(stats.get("tool_count", 0)))
    output = output.replace("__STAT_RAG__", str(stats.get("retrieval_count", 0)))
    output = output.replace("__STAT_STORAGE__", str(stats.get("storage_backend", "memory")))

    return output
