import { BrowserRouter, Routes, Route } from "react-router-dom";
import { useSession } from "./api/hooks";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Jobs } from "./pages/Jobs";
import { JobDetail } from "./pages/JobDetail";
import { Login } from "./pages/Login";
import { Profile } from "./pages/Profile";
import { Settings } from "./pages/Settings";

export function App() {
  const { data: session, isPending, error } = useSession();

  // Gate before the router, not inside it: an unauthenticated app would
  // otherwise mount every page, fire their queries, and paint 401 banners over
  // the login form.
  if (isPending) return <div className="app-boot">Loading…</div>;

  // `/auth/session` is public and answers 200 either way, so an error here means
  // the API is unreachable. The login screen is still what to show: it is the
  // only screen that reads nothing and it surfaces the transport error.
  if (error || !session) return <Login />;
  if (session.required && !session.authenticated) return <Login />;

  return (
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/jobs/:id" element={<JobDetail />} />
          <Route path="/profile" element={<Profile />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  );
}
