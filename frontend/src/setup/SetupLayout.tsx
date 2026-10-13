import type { ReactNode } from "react";
import { motion } from "framer-motion";
import { useAuth } from "../auth/useAuth";
import { Logo } from "../components/brand";

interface Props {
  headline: string;
  stepIndex: number;
  total: number;
  children: ReactNode;
}

export function SetupLayout({ headline, stepIndex, total, children }: Props) {
  const { logout } = useAuth();
  return (
    <div className="setup-root">
      <div className="setup-left">
        <div className="setup-assistant">
          <span className="setup-assistant-avatar">
            <Logo variant="mark" theme="light" size={48} className="setup-assistant-logo" />
          </span>
          <span>
            <div className="setup-assistant-name">Tailrd</div>
            <div className="setup-assistant-sub">Your job search, tailored</div>
          </span>
        </div>
        <motion.h1
          key={headline}
          className="setup-headline setup-anim"
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.25 }}
          dangerouslySetInnerHTML={{ __html: headline }}
        />
      </div>
      <div className="setup-right">
        <button className="setup-logout" onClick={logout}>Logout</button>
        <div className="setup-dots" aria-label={`Step ${stepIndex + 1} of ${total}`}>
          {Array.from({ length: total }).map((_, i) => (
            <span key={i} className={`setup-dot${i === stepIndex ? " active" : i < stepIndex ? " done" : ""}`} />
          ))}
        </div>
        <motion.div
          key={stepIndex}
          className="setup-form setup-anim"
          initial={{ opacity: 0, x: 16 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.22 }}
        >
          {children}
        </motion.div>
      </div>
    </div>
  );
}
