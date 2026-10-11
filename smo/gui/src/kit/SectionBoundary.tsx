/** An error boundary around one box of a page (STRUCTURE.md rule 1): a crash while rendering a section shows an error with a retry in
 * that box and the rest of the page keeps working. The section id (`alarms.table`) goes on the wrapper as `data-section`, the id used in
 * logs, tests and the page's README. */
import { Component, type ErrorInfo, type ReactNode } from "react";

import { useT } from "../i18n";

interface Props { id: string; children: ReactNode }
interface State { error: Error | null }

/** The error box of a crashed section (a function, so it can read the translator). */
function SectionError({ id, error, onRetry }: { id: string; error: Error; onRetry: () => void }) {
  const t = useT();
  return (
    <section className="card" data-section={id}>
      <div className="error-box row between" role="alert">
        <span>{t("kit.boundary.failed", { error: error.message })}</span>
        <button type="button" className="btn small" onClick={onRetry}>{t("kit.retry")}</button>
      </div>
    </section>
  );
}

/** The boundary. Retry re-mounts the section. */
export class SectionBoundary extends Component<Props, State> {
  state: State = { error: null };

  /** Catches a render error of a child section. */
  static getDerivedStateFromError(error: Error): State { return { error }; }

  /** Logs the crash with the section id, so a report names the box that failed. */
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`section ${this.props.id} crashed`, error, info.componentStack);
  }

  /** The section, or its error box. */
  render() {
    if (this.state.error) {
      return <SectionError id={this.props.id} error={this.state.error} onRetry={() => this.setState({ error: null })} />;
    }
    return this.props.children;
  }
}
