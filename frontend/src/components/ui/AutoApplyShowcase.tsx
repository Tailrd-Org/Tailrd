import { useState, useEffect, useRef } from "react";
import { motion, useInView, AnimatePresence } from "framer-motion";
import { Logo } from "../brand";
import "./AutoApplyShowcase.css";

const JOBS = [
  {
    title: "Backend Engineer",
    company: "Netflix",
    type: "Full Time",
    location: "Los Gatos, CA",
    salary: "$130K - $180K",
    favicon: "https://www.google.com/s2/favicons?domain=netflix.com&sz=32",
  },
  {
    title: "DevOps Engineer",
    company: "Spotify",
    type: "Full Time",
    location: "New York, NY",
    salary: "$120K - $160K",
    favicon: "https://www.google.com/s2/favicons?domain=spotify.com&sz=32",
  },
  {
    title: "Cloud Engineer",
    company: "Microsoft",
    type: "Full Time",
    location: "Seattle, WA",
    salary: "$140K - $190K",
    favicon: "https://www.google.com/s2/favicons?domain=microsoft.com&sz=32",
  },
  {
    title: "ML Engineer",
    company: "Google",
    type: "Full Time",
    location: "Mountain View, CA",
    salary: "$150K - $200K",
    favicon: "https://www.google.com/s2/favicons?domain=google.com&sz=32",
  },
  {
    title: "Frontend Developer",
    company: "Shopify",
    type: "Remote",
    location: "Ottawa, ON",
    salary: "$110K - $150K",
    favicon: "https://www.google.com/s2/favicons?domain=shopify.com&sz=32",
  },
];

const PROCESSING_STEPS = [
  "Finding keywords in the posting...",
  "Suggesting résumé edits...",
  "Gathering cover letter ideas...",
  "Filling fields from your profile...",
  "Ready for your review",
];

const COMPANIES = [
  { name: "Google", favicon: "https://www.google.com/s2/favicons?domain=google.com&sz=32", time: "Just now" },
  { name: "Netflix", favicon: "https://www.google.com/s2/favicons?domain=netflix.com&sz=32", time: "1 min ago" },
  { name: "Shopify", favicon: "https://www.google.com/s2/favicons?domain=shopify.com&sz=32", time: "2 min ago" },
];

export default function AutoApplyShowcase() {
  const ref = useRef<HTMLDivElement>(null);
  const isInView = useInView(ref, { once: false, amount: 0.3 });
  const [cycle, setCycle] = useState(0);
  const [visibleSteps, setVisibleSteps] = useState<number[]>([]);
  const [activeJobIndex, setActiveJobIndex] = useState(0);
  const [isProcessing, setIsProcessing] = useState(false);

  // Main animation loop
  useEffect(() => {
    if (!isInView) return;

    let timeout: ReturnType<typeof setTimeout>;
    let stepIndex = 0;

    const runCycle = () => {
      setIsProcessing(true);
      setVisibleSteps([]);
      stepIndex = 0;

      const showNextStep = () => {
        if (stepIndex < PROCESSING_STEPS.length) {
          setVisibleSteps((prev) => [...prev, stepIndex]);
          stepIndex++;
          timeout = setTimeout(showNextStep, 800);
        } else {
          // Cycle complete: wait then restart
          timeout = setTimeout(() => {
            setIsProcessing(false);
            setActiveJobIndex((prev) => (prev + 1) % JOBS.length);
            timeout = setTimeout(() => {
              setCycle((c) => c + 1);
              runCycle();
            }, 600);
          }, 1200);
        }
      };

      timeout = setTimeout(showNextStep, 500);
    };

    // Start first cycle after a short delay
    timeout = setTimeout(runCycle, 800);

    return () => clearTimeout(timeout);
  }, [isInView, cycle]);

  return (
    <div className="autoapply-showcase" ref={ref}>
      {/* Left: Scrolling job cards */}
      <div className="showcase-jobs-column">
        <div className="showcase-jobs-scroll">
          <AnimatePresence mode="popLayout">
            {JOBS.map((job, i) => {
              const offset = (i - activeJobIndex + JOBS.length) % JOBS.length;
              if (offset > 2) return null;
              return (
                <motion.div
                  key={`${job.company}-${cycle}-${i}`}
                  className={`showcase-job-card ${offset === 0 ? "active" : ""}`}
                  initial={{ opacity: 0, y: 40, scale: 0.95 }}
                  animate={{
                    opacity: offset > 2 ? 0 : 1 - offset * 0.2,
                    y: offset * 8,
                    scale: 1 - offset * 0.03,
                    zIndex: 10 - offset,
                  }}
                  exit={{ opacity: 0, y: -60, scale: 0.9 }}
                  transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
                  layout
                >
                  <div className="sjc-content">
                    <div className="sjc-info">
                      <div className="sjc-title">{job.title}</div>
                      <div className="sjc-company">
                        {job.company} • {job.type}
                      </div>
                      <div className="sjc-meta">
                        <span className="sjc-location">📍 {job.location}</span>
                        <span className="sjc-salary">{job.salary}</span>
                      </div>
                    </div>
                    <div className="sjc-logo">
                      <img src={job.favicon} alt={job.company} />
                    </div>
                  </div>
                  {/* Scanning bar */}
                  {offset === 0 && isProcessing && (
                    <motion.div
                      className="sjc-scan-bar"
                      initial={{ left: "0%" }}
                      animate={{ left: ["0%", "100%", "0%"] }}
                      transition={{
                        duration: 2,
                        repeat: Infinity,
                        ease: "easeInOut",
                      }}
                    />
                  )}
                </motion.div>
              );
            })}
          </AnimatePresence>
        </div>
      </div>

      {/* Right: Processing panel */}
      <div className="showcase-process-panel">
        <div className="spp-header">
          <Logo variant="mark" theme="light" size={32} className="spp-logo" />
          <div className="spp-header-info">
            <div className="spp-company">{JOBS[activeJobIndex].company}</div>
            <div className="spp-role">{JOBS[activeJobIndex].title}</div>
          </div>
          <div className="spp-close">×</div>
        </div>

        <div className="spp-body">
          {visibleSteps.map((stepIdx) => (
            <motion.div
              key={`step-${stepIdx}-${cycle}`}
              className={`spp-step ${stepIdx === PROCESSING_STEPS.length - 1 ? "done" : ""}`}
              initial={{ opacity: 0, x: -10 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.3 }}
            >
              <span className="spp-step-icon">
                {stepIdx === PROCESSING_STEPS.length - 1 ? "✓" : "–"}
              </span>
              {PROCESSING_STEPS[stepIdx]}
            </motion.div>
          ))}
          {visibleSteps.length === 0 && (
            <div className="spp-waiting">
              <div className="spp-waiting-dots">
                <span></span><span></span><span></span>
              </div>
            </div>
          )}
        </div>

        {/* Bottom company badges */}
        <div className="spp-companies">
          {COMPANIES.map((c) => (
            <div key={c.name} className="spp-company-badge">
              <img src={c.favicon} alt={c.name} />
              <div className="spp-badge-info">
                <span className="spp-badge-name">{c.name}</span>
                <span className="spp-badge-time">{c.time}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
