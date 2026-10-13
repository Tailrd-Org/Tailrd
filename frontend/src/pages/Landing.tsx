import { useState, useEffect } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import SiteHeader from "../components/site/SiteHeader";
import SiteFooter from "../components/site/SiteFooter";
import { motion } from "framer-motion";
import TypewriterText from "../components/ui/typewriter-text";
import AnimatedSection, {
  StaggerContainer,
  StaggerItem,
  FloatingElement,
  AnimatedCounter,
} from "../components/ui/animated-section";
import AutoApplyShowcase from "../components/ui/AutoApplyShowcase";
import { TiltCard } from "../components/ui/tilt-card";
import PricingTiers from "../components/PricingTiers";
import { Logo } from "../components/brand";
import "./Landing.css";

const SUCCESS_STORIES = [
  {
    img: "/interview_offer.png",
    alt: "Interview invitation letter",
    badge: "Interview Secured ✓",
    initials: "FA",
    name: "Ahmed A., applied for a Software Engineer role in Ottawa",
    quote: '"Tailrd helped me land this interview in under a week."',
  },
  {
    img: "/interview_offer_2.png",
    alt: "Second interview offer letter",
    badge: "Offer Received ✓",
    initials: "TR",
    name: "Tristan R., applied for a Software Developer role in Ottawa",
    quote: "",
  },
  {
    img: "/interview_offer_3.png",
    alt: "Third interview offer letter",
    badge: "Interview Secured ✓",
    initials: "MK",
    name: "Mohamed K., applied for a Data Analyst role in Toronto",
    quote: '"Tailrd matched me perfectly. Got the call within days."',
  },
];

function SuccessStoryCarousel() {
  const [current, setCurrent] = useState(0);
  const [animating, setAnimating] = useState(false);
  const [dir, setDir] = useState<"left" | "right">("right");

  function goTo(index: number, direction: "left" | "right") {
    if (animating || index === current) return;
    setDir(direction);
    setAnimating(true);
    setTimeout(() => {
      setCurrent(index);
      setAnimating(false);
    }, 320);
  }

  const prev = () => goTo((current - 1 + SUCCESS_STORIES.length) % SUCCESS_STORIES.length, "left");
  const next = () => goTo((current + 1) % SUCCESS_STORIES.length, "right");

  const story = SUCCESS_STORIES[current];

  return (
    <div className="success-story-card-wrapper">
      <TiltCard className="success-story-card">
        <div className="success-story-badge">{story.badge}</div>
        <div
          className={`success-story-letter-scroll story-slide${animating ? ` story-slide-out-${dir}` : " story-slide-in"}`}
        >
          <img
            src={story.img}
            alt={story.alt}
            className="success-story-letter-img"
          />
        </div>
      </TiltCard>

      {/* Navigation */}
      <div className="success-story-nav">
        <button
          className="story-nav-btn"
          onClick={prev}
          aria-label="Previous letter"
        >
          ‹
        </button>
        <div className="story-nav-dots">
          {SUCCESS_STORIES.map((_, i) => (
            <button
              key={i}
              className={`story-nav-dot${i === current ? " active" : ""}`}
              onClick={() => goTo(i, i > current ? "right" : "left")}
              aria-label={`Go to letter ${i + 1}`}
            />
          ))}
        </div>
        <button
          className="story-nav-btn"
          onClick={next}
          aria-label="Next letter"
        >
          ›
        </button>
      </div>

      <div className="success-story-caption">
        <div className="success-story-avatar">
          <span className="success-story-initials">{story.initials}</span>
        </div>
        <div className="success-story-meta">
          <span className="success-story-name">{story.name}</span>
          <div className="success-story-stars">★★★★★</div>
          {story.quote && <span className="success-story-quote">{story.quote}</span>}
        </div>
      </div>
    </div>
  );
}

export default function Landing() {
  const navigate = useNavigate();
  const location = useLocation();
  useEffect(() => {
    if (!location.hash) return;
    const el = document.getElementById(location.hash.slice(1));
    if (el) requestAnimationFrame(() => el.scrollIntoView({ behavior: "smooth", block: "start" }));
  }, [location.hash]);
  const [openFaq, setOpenFaq] = useState<number | null>(null);

  const faqs = [
    { q: "How is Tailrd different from other job platforms like LinkedIn?", a: "Tailrd matches you with jobs based on your real skills, fills the repetitive fields of an application from your profile, and offers résumé suggestions and talking points for screening questions and cover letters. You review everything and submit each application yourself. Tailrd never submits for you." },
    { q: "Will Tailrd share my personal information?", a: "We share it with the service providers that help us run Tailrd and, at your direction, with employers when you submit an application yourself. We do not sell your personal information for money. Our Privacy Policy has the full details." },
    { q: "Is Tailrd free to use?", a: "Yes! You can start with our free tier, which includes 10 assisted applications per day. Upgrade to Pro for unlimited assisted applications and more AI suggestions." },
    { q: "How does assisted apply work?", a: "The Tailrd Chrome extension fills the repetitive fields of an application from your profile and attaches your résumé. For screening questions it offers talking points, and you write the answer. You review the whole page and press Submit yourself. Tailrd never submits for you." },
    { q: "What job platforms does Tailrd support?", a: "The Tailrd Chrome extension works on the application pages of common hiring systems, including Greenhouse, Lever, Workday, Ashby, and Workable. Where a page isn't supported, you can still use your Tailrd profile and suggestions by hand." },
    { q: "I have more questions!", a: "Reach out to us at support@tailrd.app and we'll get back to you within 24 hours." },
  ];

  return (
    <div className="landing">
      {/* Nav */}
      <SiteHeader />

      {/* Hero: Full viewport height */}
      <section className="hero">
        <div className="hero-bg"></div>
        {/* Floating decorative orbs */}
        <FloatingElement className="hero-orb hero-orb-1" duration={4} distance={15} />
        <FloatingElement className="hero-orb hero-orb-2" duration={5} distance={12} />
        <FloatingElement className="hero-orb hero-orb-3" duration={6} distance={8} />
        <div className="hero-content">
          {/* Entry offsets are capped at the hero's own 24px side gutter: the
              columns fill their grid tracks, so anything bigger translates the
              content past the viewport edge and flashes a horizontal scrollbar
              for the length of the animation. */}
          <motion.div
            className="hero-left"
            initial={{ opacity: 0, x: -24 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.9, delay: 0.2, ease: [0.16, 1, 0.3, 1] }}
          >
            <div className="hero-mockup">
              <div className="mockup-window">
                <div className="mockup-dots">
                  <span></span><span></span><span></span>
                </div>
                <div className="mockup-body">
                  <div className="mockup-sidebar">
                    <div className="mockup-sidebar-item active">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#5B5BFF" strokeWidth="2"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
                    </div>
                    <div className="mockup-sidebar-item">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#9ca3af" strokeWidth="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                    </div>
                    <div className="mockup-sidebar-item">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#9ca3af" strokeWidth="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                    </div>
                    <div className="mockup-sidebar-item">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#9ca3af" strokeWidth="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                    </div>
                  </div>
                  <div className="mockup-main">
                    <div className="mockup-header">
                      <div className="mockup-header-top">
                        
                      </div>
                      <div className="mockup-search">
                        <span className="mockup-search-icon">🔍</span>
                        <span className="mockup-search-text">Search jobs, companies...</span>
                      </div>
                      <div className="mockup-filter-row">
                        <span className="mockup-filter-chip active">All Jobs</span>
                        <span className="mockup-filter-chip">Remote</span>
                        <span className="mockup-filter-chip">Canada</span>
                        <span className="mockup-filter-chip">Full-time</span>
                      </div>
                    </div>
                    <motion.div
                      className="mockup-card"
                      initial={{ opacity: 0, y: 20 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: 0.6, duration: 0.5 }}
                    >
                      <div className="mockup-card-icon">
                        <img src="https://www.google.com/s2/favicons?domain=google.com&sz=32" alt="Google" />
                      </div>
                      <div className="mockup-card-content">
                        <div className="mockup-card-title">Senior Software Engineer</div>
                        <div className="mockup-card-sub">Google • Mountain View, CA</div>
                        <div className="mockup-tags">
                          <span className="mockup-tag">Python</span>
                          <span className="mockup-tag">React</span>
                          <span className="mockup-tag">AWS</span>
                        </div>
                      </div>
                      <div className="mockup-match">95%</div>
                    </motion.div>
                    <motion.div
                      className="mockup-card"
                      initial={{ opacity: 0, y: 20 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: 0.9, duration: 0.5 }}
                    >
                      <div className="mockup-card-icon mini">
                        <img src="https://www.google.com/s2/favicons?domain=meta.com&sz=32" alt="Meta" />
                      </div>
                      <div className="mockup-card-content">
                        <div className="mockup-card-title">Backend Engineer</div>
                        <div className="mockup-card-sub">Meta • Remote</div>
                        <div className="mockup-tags">
                          <span className="mockup-tag">Java</span>
                          <span className="mockup-tag">Microservices</span>
                        </div>
                      </div>
                      <div className="mockup-match">88%</div>
                    </motion.div>
                    <motion.div
                      className="mockup-card"
                      initial={{ opacity: 0, y: 20 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: 1.2, duration: 0.5 }}
                    >
                      <div className="mockup-card-icon mini">
                        <img src="https://www.google.com/s2/favicons?domain=shopify.com&sz=32" alt="Shopify" />
                      </div>
                      <div className="mockup-card-content">
                        <div className="mockup-card-title">Full Stack Developer</div>
                        <div className="mockup-card-sub">Shopify • Ottawa, ON</div>
                        <div className="mockup-tags">
                          <span className="mockup-tag">TypeScript</span>
                          <span className="mockup-tag">Node.js</span>
                        </div>
                      </div>
                      <div className="mockup-match">91%</div>
                    </motion.div>
                  </div>
                </div>
              </div>
            </div>
          </motion.div>
          <motion.div
            className="hero-right"
            initial={{ opacity: 0, x: 24 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.9, delay: 0.3, ease: [0.16, 1, 0.3, 1] }}
          >
            <motion.p
              className="hero-eyebrow"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.5, duration: 0.6 }}
            >
              No More Solo Job Hunting
            </motion.p>
            <h1 className="hero-headline">
              <TypewriterText
                text="Your application, your words."
                speed={90}
                delay={800}
                showCursor={true}
                className="hero-headline-typed"
              />
            </h1>
            <motion.p
              className="hero-sub"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 2.2, duration: 0.6 }}
            >
              Find roles that fit, fill the repetitive fields from your profile,
              and get AI suggestions you review, edit, and own.
            </motion.p>
            <motion.div
              className="hero-actions"
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ delay: 2.6, duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
            >
              <button className="btn-cta-hero" onClick={() => navigate("/app")}>
                TRY FOR FREE
              </button>
            </motion.div>
          </motion.div>
        </div>
      </section>

      {/* Live assisted-apply demo: Top Showcase */}
      <section className="showcase-section showcase-demo-section">
        <div className="showcase-demo-wrapper">
          <motion.div
            className="showcase-demo-header"
            initial={{ opacity: 0, y: 30 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7, delay: 2.8, ease: [0.16, 1, 0.3, 1] }}
          >
            <span className="showcase-demo-eyebrow">SEE IT IN ACTION</span>
            <h2 className="showcase-title">See how Tailrd works with you</h2>
            <p className="showcase-desc">
              Tailrd reads the posting, points out the keywords that matter,
              fills the repetitive fields from your profile, and leaves every
              judgment call to you.
            </p>
          </motion.div>
          <motion.div
            initial={{ opacity: 0, y: 40 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.8, delay: 3.0, ease: [0.16, 1, 0.3, 1] }}
          >
            <AutoApplyShowcase />
          </motion.div>
        </div>
      </section>

      {/* Stats Section */}
      <AnimatedSection animation="fadeUp">
        <section className="stats-section-light" id="stats">
          <div className="stats-content-light">
            <AnimatedSection className="stats-left-light" animation="fadeLeft" delay={0.1}>
              <span className="stats-eyebrow-light">OUR TRACK RECORD</span>
              <h2 className="stats-headline-light">
                <TypewriterText
                  text="Real results, not just promises."
                  speed={50}
                  triggerOnView={true}
                  showCursor={false}
                  className=""
                />
              </h2>
              <button className="btn-dark" onClick={() => navigate("#features")}>
                See how it works ↗
              </button>
            </AnimatedSection>
            <StaggerContainer className="stats-right-light" staggerDelay={0.15}>
              <StaggerItem>
                <div className="stat-card-light">
                  <div className="stat-icon-light">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#5B5BFF" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>
                    </svg>
                  </div>
                  <div className="stat-info-light">
                    <span className="stat-number-light">No. 1 choice</span>
                    <span className="stat-label-light">for <AnimatedCounter value={80} suffix="%" /> of users after first use</span>
                  </div>
                </div>
              </StaggerItem>
            </StaggerContainer>
          </div>
        </section>
      </AnimatedSection>

      {/* Feature Showcase 1: AI Job Matches */}
      <AnimatedSection animation="fadeUp">
        <section className="showcase-section" id="features">
          <div className="showcase-content">
            <AnimatedSection className="showcase-text" animation="fadeLeft" delay={0.2}>
              <h2 className="showcase-title">
                <TypewriterText
                  text="Your Skills, Ranked & Matched"
                  speed={45}
                  triggerOnView={true}
                  showCursor={false}
                  className=""
                />
              </h2>
              <p className="showcase-desc">
                Our AI reads the job description and your resume side by side,
                then scores how well you fit, so you decide where to spend
                your time.
              </p>
              <button className="btn-dark" onClick={() => navigate("/app")}>See My Fit Score</button>
            </AnimatedSection>
            <AnimatedSection className="showcase-visual" animation="fadeRight" delay={0.3}>
              <div className="showcase-card match-card">
                <div className="match-card-header">
                  <span className="match-time">1 hour ago</span>
                  <div className="match-score-circle big">
                    <span className="match-score-num">95</span>
                    <span className="match-score-pct">%</span>
                    <span className="match-score-label">Overall</span>
                  </div>
                </div>
                <div className="match-card-job">
                  <div className="match-job-icon">
                    <img src="https://www.google.com/s2/favicons?domain=runway.com&sz=32" alt="Runway" style={{ width: '24px', height: '24px', borderRadius: '4px' }} />
                  </div>
                  <div>
                    <div className="match-job-title">Senior Data Analyst</div>
                    <div className="match-job-company">Runway • New York, NY</div>
                  </div>
                </div>
                <div className="match-scores-row">
                  <div className="match-mini-score">
                    <div className="mini-score-circle">95<span>%</span></div>
                    <span>Exp. Level</span>
                  </div>
                  <div className="match-mini-score">
                    <div className="mini-score-circle">93<span>%</span></div>
                    <span>Skill</span>
                  </div>
                  <div className="match-mini-score">
                    <div className="mini-score-circle">96<span>%</span></div>
                    <span>Industry Exp.</span>
                  </div>
                </div>
                <div className="match-fit">
                  <div className="match-fit-title">Why You Are A Good Fit</div>
                  <div className="match-fit-tag good">✓ Experience Level</div>
                  <div className="match-fit-tag good">✓ Technical Skills</div>
                  <div className="match-fit-tag warn">✕ Education</div>
                </div>
              </div>
            </AnimatedSection>
          </div>
        </section>
      </AnimatedSection>

      {/* Feature Showcase 2: Fill from profile */}
      <AnimatedSection animation="fadeUp">
        <section className="showcase-section showcase-reverse">
          <div className="showcase-content">
            <AnimatedSection className="showcase-text" animation="fadeRight" delay={0.2}>
              <h2 className="showcase-title">
                <TypewriterText
                  text="Less retyping, more judgment"
                  speed={45}
                  triggerOnView={true}
                  showCursor={false}
                  className=""
                />
              </h2>
              <p className="showcase-desc">
                Tailrd fills the fields your profile already answers, points out
                what a question is asking, and offers talking points. You write
                the answers, review the page, and press Submit yourself.
              </p>
              <button className="btn-dark" onClick={() => navigate("/app")}>See how it works</button>
            </AnimatedSection>
            <AnimatedSection className="showcase-visual" animation="fadeLeft" delay={0.3}>
              <div className="showcase-card autofill-card">
                <div className="autofill-header">
                  <Logo variant="mark" theme="light" size={28} className="autofill-logo" />
                  <span className="autofill-brand">Tailrd</span>
                </div>
                <div className="autofill-job">
                  <div className="autofill-job-icon">
                    <img src="https://www.google.com/s2/favicons?domain=runway.com&sz=32" alt="Runway" style={{ width: '24px', height: '24px', borderRadius: '4px' }} />
                  </div>
                  <div className="autofill-job-info">
                    <div className="autofill-company">Runway • Artificial Intelligence(AI)</div>
                    <div className="autofill-title">Product Manager, Analytics</div>
                    <div className="autofill-meta">11hr ago · 49 applicants</div>
                  </div>
                  <div className="autofill-score">80%</div>
                </div>
                <button className="autofill-btn">Fill from profile</button>
                <div className="autofill-credits">8 Remaining Credits</div>
                <div className="autofill-progress">
                  <div className="autofill-progress-label">
                    <strong>Completion</strong> <span>83%</span>
                  </div>
                  <div className="autofill-progress-bar">
                    <div className="autofill-progress-fill"></div>
                  </div>
                </div>
                <div className="autofill-fields">
                  <div className="autofill-field-title">Required (10/12 filled)</div>
                  <div className="autofill-field"><span className="field-check">✓</span> Name</div>
                  <div className="autofill-field"><span className="field-check">✓</span> Phone</div>
                  <div className="autofill-field"><span className="field-check">✓</span> Email</div>
                  <div className="autofill-field"><span className="field-check">✓</span> LinkedIn URL</div>
                  <div className="autofill-field"><span className="field-dash">-</span> Experience</div>
                  <div className="autofill-field"><span className="field-dash">-</span> Cover Letter</div>
                </div>
              </div>
            </AnimatedSection>
          </div>
        </section>
      </AnimatedSection>

      {/* Feature Showcase 3: Resume Tailoring */}
      <AnimatedSection animation="fadeUp">
        <section className="showcase-section">
          <div className="showcase-content">
            <AnimatedSection className="showcase-text" animation="fadeLeft" delay={0.2}>
              <h2 className="showcase-title">
                <TypewriterText
                  text="Suggestions for every role, decisions by you"
                  speed={45}
                  triggerOnView={true}
                  showCursor={false}
                  className=""
                />
              </h2>
              <p className="showcase-desc">
                For each job, Tailrd suggests edits that mirror the posting's
                language and surface your relevant wins. You choose which
                changes stay.
              </p>
              <ul className="showcase-bullets">
                <li>✓ Points out keywords from the posting</li>
                <li>✓ Suggests which sections to lead with</li>
                <li>✓ Flags where a metric would help (you supply the number)</li>
                <li>✓ You accept or reject every change</li>
              </ul>
              <button className="btn-dark" onClick={() => navigate("/app")}>See suggestions</button>
            </AnimatedSection>
            <AnimatedSection className="showcase-visual" animation="fadeRight" delay={0.3}>
              <div className="showcase-card resume-card">
                <div className="resume-header">
                  <div className="resume-star">✦</div>
                  <div className="resume-name">Jamie Parker</div>
                  <div className="resume-score-badge">
                    <span className="resume-score-num">9.0</span>
                    <span className="resume-score-label">EXCELLENT</span>
                  </div>
                </div>
                <div className="resume-target-job">
                  <span className="resume-target-label">Suggestions for:</span>
                  <span className="resume-target-value">Senior Software Engineer at Google</span>
                </div>
                <div className="resume-section">
                  <div className="resume-section-title">PROFESSIONAL SUMMARY</div>
                  <div className="resume-lines">
                    <div className="resume-line"></div>
                    <div className="resume-line short"></div>
                  </div>
                </div>
                <div className="resume-section">
                  <div className="resume-section-title">SKILLS</div>
                  <div className="resume-skill-tags">
                    <span className="resume-skill-tag highlighted">Python</span>
                    <span className="resume-skill-tag highlighted">React</span>
                    <span className="resume-skill-tag highlighted">AWS</span>
                    <span className="resume-skill-tag">Docker</span>
                    <span className="resume-skill-tag">TypeScript</span>
                    <span className="resume-skill-tag highlighted">System Design</span>
                  </div>
                </div>
                <div className="resume-section">
                  <div className="resume-section-title">EXPERIENCE</div>
                  <div className="resume-exp-item">
                    <div className="resume-exp-header">
                      <span className="resume-exp-role">Lead Engineer</span>
                      <span className="resume-exp-date">2022 - Present</span>
                    </div>
                    <div className="resume-exp-company">TechCorp Inc.</div>
                    <div className="resume-lines">
                      <div className="resume-line"></div>
                      <div className="resume-line short"></div>
                    </div>
                  </div>
                  <div className="resume-exp-item">
                    <div className="resume-exp-header">
                      <span className="resume-exp-role">Software Engineer</span>
                      <span className="resume-exp-date">2019 - 2022</span>
                    </div>
                    <div className="resume-exp-company">StartupXYZ</div>
                    <div className="resume-lines">
                      <div className="resume-line"></div>
                      <div className="resume-line short"></div>
                    </div>
                  </div>
                </div>
                <div className="resume-section">
                  <div className="resume-section-title">EDUCATION</div>
                  <div className="resume-lines">
                    <div className="resume-line"></div>
                  </div>
                </div>
                <div className="resume-enhancements">
                  <span className="enhancement-tag">✦ Summary suggestion</span>
                  <span className="enhancement-tag">✦ Relevant skills flagged</span>
                  <span className="enhancement-tag">✦ Keywords from the posting</span>
                  <span className="enhancement-tag">✦ Recent experience suggestions</span>
                </div>
              </div>
            </AnimatedSection>
          </div>
        </section>
      </AnimatedSection>

      {/* Trusted By Companies: Continuous Carousel */}
      <AnimatedSection animation="fadeUp">
        <section className="companies-section">
          <p className="companies-label">Trusted by job seekers applying to</p>
          <div className="companies-carousel-wrapper">
            <div className="companies-carousel-track">
              {/* First set */}
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=google.com&sz=32" alt="Google" className="company-favicon" />
                <span className="company-name">Google</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=meta.com&sz=32" alt="Meta" className="company-favicon" />
                <span className="company-name">Meta</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=amazon.com&sz=32" alt="Amazon" className="company-favicon" />
                <span className="company-name">Amazon</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=microsoft.com&sz=32" alt="Microsoft" className="company-favicon" />
                <span className="company-name">Microsoft</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=apple.com&sz=32" alt="Apple" className="company-favicon" />
                <span className="company-name">Apple</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=netflix.com&sz=32" alt="Netflix" className="company-favicon" />
                <span className="company-name">Netflix</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=shopify.com&sz=32" alt="Shopify" className="company-favicon" />
                <span className="company-name">Shopify</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=stripe.com&sz=32" alt="Stripe" className="company-favicon" />
                <span className="company-name">Stripe</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=airbnb.com&sz=32" alt="Airbnb" className="company-favicon" />
                <span className="company-name">Airbnb</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=uber.com&sz=32" alt="Uber" className="company-favicon" />
                <span className="company-name">Uber</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=salesforce.com&sz=32" alt="Salesforce" className="company-favicon" />
                <span className="company-name">Salesforce</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=spotify.com&sz=32" alt="Spotify" className="company-favicon" />
                <span className="company-name">Spotify</span>
              </span>
              {/* Duplicate set for seamless loop */}
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=google.com&sz=32" alt="Google" className="company-favicon" />
                <span className="company-name">Google</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=meta.com&sz=32" alt="Meta" className="company-favicon" />
                <span className="company-name">Meta</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=amazon.com&sz=32" alt="Amazon" className="company-favicon" />
                <span className="company-name">Amazon</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=microsoft.com&sz=32" alt="Microsoft" className="company-favicon" />
                <span className="company-name">Microsoft</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=apple.com&sz=32" alt="Apple" className="company-favicon" />
                <span className="company-name">Apple</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=netflix.com&sz=32" alt="Netflix" className="company-favicon" />
                <span className="company-name">Netflix</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=shopify.com&sz=32" alt="Shopify" className="company-favicon" />
                <span className="company-name">Shopify</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=stripe.com&sz=32" alt="Stripe" className="company-favicon" />
                <span className="company-name">Stripe</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=airbnb.com&sz=32" alt="Airbnb" className="company-favicon" />
                <span className="company-name">Airbnb</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=uber.com&sz=32" alt="Uber" className="company-favicon" />
                <span className="company-name">Uber</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=salesforce.com&sz=32" alt="Salesforce" className="company-favicon" />
                <span className="company-name">Salesforce</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=spotify.com&sz=32" alt="Spotify" className="company-favicon" />
                <span className="company-name">Spotify</span>
              </span>
              {/* Third set for seamless loop */}
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=google.com&sz=32" alt="Google" className="company-favicon" />
                <span className="company-name">Google</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=meta.com&sz=32" alt="Meta" className="company-favicon" />
                <span className="company-name">Meta</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=amazon.com&sz=32" alt="Amazon" className="company-favicon" />
                <span className="company-name">Amazon</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=microsoft.com&sz=32" alt="Microsoft" className="company-favicon" />
                <span className="company-name">Microsoft</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=apple.com&sz=32" alt="Apple" className="company-favicon" />
                <span className="company-name">Apple</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=netflix.com&sz=32" alt="Netflix" className="company-favicon" />
                <span className="company-name">Netflix</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=shopify.com&sz=32" alt="Shopify" className="company-favicon" />
                <span className="company-name">Shopify</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=stripe.com&sz=32" alt="Stripe" className="company-favicon" />
                <span className="company-name">Stripe</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=airbnb.com&sz=32" alt="Airbnb" className="company-favicon" />
                <span className="company-name">Airbnb</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=uber.com&sz=32" alt="Uber" className="company-favicon" />
                <span className="company-name">Uber</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=salesforce.com&sz=32" alt="Salesforce" className="company-favicon" />
                <span className="company-name">Salesforce</span>
              </span>
              <span className="company-logo">
                <img src="https://www.google.com/s2/favicons?domain=spotify.com&sz=32" alt="Spotify" className="company-favicon" />
                <span className="company-name">Spotify</span>
              </span>
            </div>
          </div>
        </section>
      </AnimatedSection>

      {/* Success Story */}
      <AnimatedSection animation="fadeUp">
        <section className="success-story-section" id="success-story">
          <div className="success-story-inner">
            <div className="success-story-headline">
              <span className="success-story-eyebrow">PROOF, NOT PROMISES</span>
              <h2 className="success-story-title">Real Results from Real Users</h2>
              <p className="success-story-sub">Our users don't just apply. They get invited to interview.</p>
            </div>

            <SuccessStoryCarousel />
          </div>
        </section>
      </AnimatedSection>

      {/* Pricing */}
      <AnimatedSection animation="fadeUp">
        <section className="section" id="pricing">
          <h2 className="section-title">Simple Pricing</h2>
          <p className="section-sub">Start free, upgrade when you're ready. Prices in CAD.</p>
          <PricingTiers variant="teaser" />
          <div style={{ textAlign: "center", marginTop: 24 }}>
            <button className="btn-ghost" onClick={() => navigate("/pricing")}>See full pricing →</button>
          </div>
        </section>
      </AnimatedSection>

      {/* FAQ */}
      <AnimatedSection animation="fadeUp">
        <section className="faq-section" id="faq">
          <h2 className="faq-title">
            <TypewriterText
              text="Frequently Asked Questions"
              speed={40}
              triggerOnView={true}
              showCursor={false}
              className=""
            />
          </h2>
          <StaggerContainer className="faq-list" staggerDelay={0.1}>
            {faqs.map((faq, i) => (
              <StaggerItem key={i}>
                <div className={`faq-item ${openFaq === i ? "open" : ""}`}>
                  <button className="faq-question" onClick={() => setOpenFaq(openFaq === i ? null : i)}>
                    <span>{faq.q}</span>
                    <span className="faq-toggle">{openFaq === i ? "−" : "+"}</span>
                  </button>
                  {openFaq === i && (
                    <motion.div
                      className="faq-answer"
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: "auto" }}
                      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
                    >
                      {faq.a}
                    </motion.div>
                  )}
                </div>
              </StaggerItem>
            ))}
          </StaggerContainer>
        </section>
      </AnimatedSection>

      {/* Footer */}
      <SiteFooter />
    </div>
  );
}
