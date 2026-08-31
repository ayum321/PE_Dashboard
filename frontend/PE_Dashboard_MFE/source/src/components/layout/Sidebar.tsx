import React from 'react';
import { NavLink } from 'react-router-dom';
import '../../theme/dashboard.css';
import {
  ArchiveIcon,
  BatchIcon,
  BenchmarkIcon,
  ExecutiveIcon,
  FindingsIcon,
  GovernanceIcon,
  ResourceIcon,
  SettingsIcon,
  SlaMatrixIcon,
  SowIcon,
  UploadIcon,
} from '../../theme/icons';

interface NavItem {
  path: string;
  label: string;
  icon: React.ComponentType;
}

const WORKSPACE_ITEMS: NavItem[] = [
  { path: '/upload', label: 'Upload & Intake', icon: UploadIcon },
  { path: '/executive', label: 'Executive Dashboard', icon: ExecutiveIcon },
];

const ANALYSIS_ITEMS: NavItem[] = [
  { path: '/batch', label: 'Batch Review', icon: BatchIcon },
  { path: '/resource', label: 'Resource Review', icon: ResourceIcon },
  { path: '/sla-matrix', label: 'SLA Matrix', icon: SlaMatrixIcon },
  { path: '/benchmark', label: 'Performance Benchmark', icon: BenchmarkIcon },
  { path: '/sow', label: 'SOW Volume & Products', icon: SowIcon },
];

const INTELLIGENCE_ITEMS: NavItem[] = [
  { path: '/findings', label: 'PE Findings', icon: FindingsIcon },
  { path: '/governance', label: 'Governance', icon: GovernanceIcon },
  { path: '/archive', label: 'Review Registry', icon: ArchiveIcon },
  { path: '/settings', label: 'Settings', icon: SettingsIcon },
];

const NavGroup = ({ label, items }: { label: string; items: NavItem[] }) => (
  <>
    <span className="nav-section-label">{label}</span>
    {items.map(({ path, label: itemLabel, icon: Icon }) => (
      <NavLink key={path} to={path} className="nav-btn" activeClassName="active">
        <Icon />
        <span>{itemLabel}</span>
      </NavLink>
    ))}
  </>
);

export function Sidebar() {
  return (
    <aside
      style={{
        width: 240,
        height: '100vh',
        position: 'sticky',
        top: 0,
        alignSelf: 'flex-start',
        background: '#06091a',
        borderRight: '1px solid #1a2850',
        display: 'flex',
        flexDirection: 'column',
        flexShrink: 0,
      }}
      aria-label="Dashboard navigation"
    >
      <div className="sidebar-accent-line" />

      <div style={{ padding: '16px', borderBottom: '1px solid #1a2850' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div
            className="brand-logo"
            style={{ width: 40, height: 40, borderRadius: 12, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}
          >
            <svg viewBox="0 0 32 32" width={24} height={24} fill="none">
              <path d="M16 2 L30 16 L16 30 L2 16 Z" fill="rgba(255,255,255,0.08)" stroke="rgba(255,255,255,0.2)" strokeWidth={0.5} />
              <text x="5.5" y="21" fontFamily="'Sora','Inter',sans-serif" fontSize="13" fontWeight={800} fill="white" letterSpacing="-0.5">
                PE
              </text>
            </svg>
          </div>
          <div style={{ minWidth: 0 }}>
            <div className="brand-name">PE Audit</div>
            <div className="brand-sub">Control Tower</div>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 12 }}>
          <span className="metric-badge metric-badge-blue">Batch</span>
          <span className="metric-badge metric-badge-teal">SLA</span>
        </div>
      </div>

      <nav style={{ flex: 1, padding: '12px 8px', overflowY: 'auto' }}>
        <NavGroup label="Workspace" items={WORKSPACE_ITEMS} />
        <NavGroup label="Analysis" items={ANALYSIS_ITEMS} />
        <NavGroup label="Intelligence" items={INTELLIGENCE_ITEMS} />
      </nav>

      <div style={{ padding: '10px 12px', borderTop: '1px solid #1a2850', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span className="version-badge">v2.1.0</span>
      </div>
    </aside>
  );
}

