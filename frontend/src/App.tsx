import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { AppShell } from "@/components/shell/AppShell";
import { useToken } from "@/lib/session";

/** React Router v7's behaviour, adopted now so the upgrade changes nothing. */
export const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

// NFR-FE-04: route-level code splitting keeps the first load small.
const SignInScreen = lazy(() => import("@/screens/SignInScreen"));
const HomeScreen = lazy(() => import("@/screens/HomeScreen"));
const QueueScreen = lazy(() => import("@/screens/QueueScreen"));
const SearchScreen = lazy(() => import("@/screens/SearchScreen"));
const DashboardScreen = lazy(() => import("@/screens/DashboardScreen"));
const RulesScreen = lazy(() => import("@/screens/RulesScreen"));
const PayerRegistryScreen = lazy(() => import("@/screens/PayerRegistryScreen"));
const ClaimDetailScreen = lazy(() => import("@/screens/ClaimDetailScreen"));
const DenialWorkbenchScreen = lazy(() => import("@/screens/DenialWorkbenchScreen"));
const NotFoundScreen = lazy(() => import("@/screens/NotFoundScreen"));

function RequireAuth({ children }: { children: ReactNode }) {
  const token = useToken();
  const location = useLocation();
  if (!token) {
    return <Navigate to="/sign-in" replace state={{ from: location.pathname + location.search }} />;
  }
  return children;
}

export function App() {
  return (
    <Suspense fallback={null}>
      <Routes>
        <Route path="/sign-in" element={<SignInScreen />} />
        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
          <Route index element={<HomeScreen />} />
          <Route path="queues/:queueKey" element={<QueueScreen />} />
          <Route path="search" element={<SearchScreen />} />
          <Route path="claims/:claimId" element={<ClaimDetailScreen />} />
          <Route path="denials/:workItemId" element={<DenialWorkbenchScreen />} />
          <Route path="dashboard" element={<DashboardScreen />} />
          <Route path="admin/rules" element={<RulesScreen />} />
          <Route path="admin/payer-registry" element={<PayerRegistryScreen />} />
          <Route path="*" element={<NotFoundScreen />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
