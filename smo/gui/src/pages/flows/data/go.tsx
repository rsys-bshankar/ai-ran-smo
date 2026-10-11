/** A small link button used by the flow boards' step actions ("Onboard a package →"). */
import { Link } from "react-router-dom";

/** A link styled as a small button, with a trailing arrow; `label` is already in the user's language. */
export const go = (to: string, label: string) => <Link className="btn small" to={to}>{label} →</Link>;
