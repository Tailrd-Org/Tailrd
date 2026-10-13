import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "./auth/useAuth";
import { useEffect, useState } from "react";
import {
  SquaresFour,
  FileText,
  ListChecks,
  UserCircle,
  ChatCircleDots,
  Gift,
  Question,
  GearSix,
  SignOut,
  CaretLeft,
  CaretRight,
  List,
  X,
  Check,
  Copy,
  LinkedinLogo,
  XLogo,
} from "@phosphor-icons/react";
import { ApplyTrackingProvider } from "./context/ApplyTracking";
import ApplyConfirmModal from "./components/ApplyConfirmModal";
import SettingsModal from "./components/SettingsModal";
import ExtensionBanner from "./components/ExtensionBanner";
import { OnboardingProvider } from "./onboarding";
import { Logo } from "./components/brand";

export default function App() {
  const { user, logout } = useAuth();
  const { pathname } = useLocation();
  const [showReferModal, setShowReferModal] = useState(false);
  const [showSettingsModal, setShowSettingsModal] = useState(false);
  const [referCopied, setReferCopied] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  // Under 768px the sidebar is an off-canvas drawer rather than a rail.
  const [navOpen, setNavOpen] = useState(false);

  const referralCode = user?.id?.toString().slice(-8) || "tailrd";
  const referralLink = `https://www.tailrd.ca?ref=${referralCode}`;

  const copyReferLink = () => {
    navigator.clipboard.writeText(referralLink);
    setReferCopied(true);
    setTimeout(() => setReferCopied(false), 2000);
  };

  // Navigating is the whole point of opening the drawer, so close it on arrival.
  useEffect(() => setNavOpen(false), [pathname]);

  // While the drawer is over the page, the page behind it must not scroll.
  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setNavOpen(false);
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [navOpen]);

  return (
    <ApplyTrackingProvider>
    <OnboardingProvider>
    <div className={`app-layout${sidebarCollapsed ? " sidebar-collapsed" : ""}${navOpen ? " nav-open" : ""}`}>
      {/* Phone-width top bar. Hidden from 768px up, where the sidebar is visible. */}
      <header className="app-topbar">
        <button
          className="app-topbar-menu"
          onClick={() => setNavOpen(true)}
          aria-label="Open navigation menu"
          aria-expanded={navOpen}
          aria-controls="app-sidebar"
        >
          <List size={22} weight="bold" />
        </button>
        <Logo variant="mark" theme="light" size={24} decorative className="app-topbar-logo" />
        <span className="app-topbar-title">Tailrd</span>
      </header>

      <div
        className="sidebar-backdrop"
        onClick={() => setNavOpen(false)}
        aria-hidden="true"
      />

      <aside className="sidebar" id="app-sidebar">
        {/* Logo */}
        <div className="sidebar-logo">
          <Logo variant="mark" theme="light" size={28} className="sidebar-logo-img" />
          <span className="logo-text">Tailrd</span>
          <button
            className="sidebar-close"
            onClick={() => setNavOpen(false)}
            aria-label="Close navigation menu"
          >
            <X size={18} weight="bold" />
          </button>
        </div>

        {/* Collapse Toggle */}
        <button
          className="sidebar-collapse-btn"
          onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
          aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {sidebarCollapsed ? (
            <CaretRight size={16} weight="bold" />
          ) : (
            <CaretLeft size={16} weight="bold" />
          )}
        </button>

        {/* Menu Section */}
        <div className="sidebar-section">
          <span className="sidebar-section-label">MENU</span>
          <nav className="sidebar-nav">
            <NavLink to="/app" end className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
              <span className="nav-icon">
                <SquaresFour size={20} weight="duotone" />
              </span>
              <span className="nav-label">Dashboard</span>
            </NavLink>
            <NavLink to="/app/resume" className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
              <span className="nav-icon">
                <FileText size={20} weight="duotone" />
              </span>
              <span className="nav-label">Resume</span>
            </NavLink>
            <NavLink to="/app/applications" className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
              <span className="nav-icon">
                <ListChecks size={20} weight="duotone" />
              </span>
              <span className="nav-label">Applications</span>
            </NavLink>
            <NavLink to="/app/profile" className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
              <span className="nav-icon">
                <UserCircle size={20} weight="duotone" />
              </span>
              <span className="nav-label">Profile</span>
            </NavLink>
            <NavLink to="/app/interview" className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
              <span className="nav-icon">
                <ChatCircleDots size={20} weight="duotone" />
              </span>
              <span className="nav-label">Interview</span>
            </NavLink>
          </nav>
        </div>

        {/* General Section */}
        <div className="sidebar-section sidebar-section-bottom">
          <span className="sidebar-section-label">GENERAL</span>
          <nav className="sidebar-nav">
            <button className="nav-item" onClick={() => setShowReferModal(true)}>
              <span className="nav-icon">
                <Gift size={20} weight="duotone" />
              </span>
              <span className="nav-label">Refer & Earn</span>
            </button>
            <NavLink to="/app/feedback" className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
              <span className="nav-icon">
                <Question size={20} weight="duotone" />
              </span>
              <span className="nav-label">Feedback</span>
            </NavLink>
            <button className="nav-item" onClick={() => setShowSettingsModal(true)}>
              <span className="nav-icon">
                <GearSix size={20} weight="duotone" />
              </span>
              <span className="nav-label">Settings</span>
            </button>
          </nav>
        </div>

        {/* User Profile at Bottom */}
        <div className="sidebar-user">
          <div className="sidebar-user-avatar">
            {user?.profile_image_url ? (
              <img src={user.profile_image_url} alt="" className="sidebar-avatar-img" />
            ) : (
              <span className="sidebar-avatar-initials">
                {(user?.first_name?.[0] || user?.email?.[0] || "U").toUpperCase()}
              </span>
            )}
          </div>
          <div className="sidebar-user-info">
            <span className="sidebar-user-name">{user?.first_name || "User"}</span>
            <span className="sidebar-user-email">{user?.email || ""}</span>
          </div>
          <button className="sidebar-logout-btn" onClick={logout} title="Sign out" aria-label="Sign out">
            <SignOut size={18} weight="bold" />
          </button>
        </div>
      </aside>
      <main className="main-content">
        <ExtensionBanner />
        <Outlet />
      </main>

      {/* Refer & Earn Modal */}
      {showReferModal && (
        <div className="modal-overlay" onClick={() => setShowReferModal(false)}>
          <div className="modal-content refer-modal" onClick={e => e.stopPropagation()}>
            <button className="modal-close" onClick={() => setShowReferModal(false)}>
              <X size={20} weight="bold" />
            </button>
            <div className="refer-modal-icon">
              <Gift size={32} weight="duotone" />
            </div>
            <h2>Invite Friends & Earn Credits</h2>
            <p>Share Tailrd with friends. When they sign up and upload their resume, you both earn +5 AI analysis credits.</p>

            <div className="refer-modal-steps">
              <div className="refer-modal-step">
                <span className="refer-step-num">1</span>
                <span>Share your link</span>
              </div>
              <div className="refer-modal-step">
                <span className="refer-step-num">2</span>
                <span>Friend signs up</span>
              </div>
              <div className="refer-modal-step">
                <span className="refer-step-num">3</span>
                <span>Both earn credits</span>
              </div>
            </div>

            <div className="refer-modal-link">
              <input type="text" readOnly value={referralLink} className="refer-link-input" />
              <button className="refer-copy-btn" onClick={copyReferLink}>
                {referCopied ? <><Check size={15} weight="bold" /> Copied</> : <><Copy size={15} weight="bold" /> Copy</>}
              </button>
            </div>

            <div className="refer-modal-share">
              <button className="refer-share-btn refer-share-linkedin" onClick={() => window.open(`https://www.linkedin.com/sharing/share-offsite/?url=${encodeURIComponent(referralLink)}`, "_blank")}>
                <LinkedinLogo size={16} weight="fill" /> LinkedIn
              </button>
              <button className="refer-share-btn refer-share-twitter" onClick={() => window.open(`https://twitter.com/intent/tweet?text=${encodeURIComponent("Check out Tailrd - job search help for interns and new grads, with AI used responsibly.")}&url=${encodeURIComponent(referralLink)}`, "_blank")}>
                <XLogo size={16} weight="fill" /> Twitter
              </button>
            </div>
          </div>
        </div>
      )}
      {showSettingsModal && (
        <SettingsModal onClose={() => setShowSettingsModal(false)} />
      )}
      <ApplyConfirmModal />
    </div>
    </OnboardingProvider>
    </ApplyTrackingProvider>
  );
}
