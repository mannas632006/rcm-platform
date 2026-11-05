import { Component, type ErrorInfo, type ReactNode } from "react";

import { ErrorPanel } from "./ErrorPanel";

interface Props {
  /** Reset when this changes -- the route -- so leaving a broken screen recovers. */
  resetKey: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * A screen that throws while rendering takes down that screen only. The
 * shell around it -- navigation, the deadline alert, sign-out -- stays.
 */
export class ScreenErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidUpdate(previous: Props) {
    if (previous.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null });
    }
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    // No PHI in telemetry (PD-13): the message names the fault, not the data.
    console.error("screen.render_failed", error.name, info.componentStack?.split("\n")[1]);
  }

  override render() {
    if (this.state.error) {
      return (
        <div className="p-8">
          <ErrorPanel
            what="this screen"
            error={this.state.error}
            onRetry={() => {
              this.setState({ error: null });
            }}
          />
        </div>
      );
    }
    return this.props.children;
  }
}
