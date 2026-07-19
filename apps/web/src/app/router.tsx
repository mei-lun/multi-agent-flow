import { Navigate, Route, Routes } from "react-router-dom";
import { DashboardPage, InboxPage, LoginPage, ModelPage, ProjectsPage, RunPage, SettingsPage, WorkflowPage } from "./App";

export function AppRouter() {
  return <Routes>
    <Route path="/login" element={<LoginPage />} />
    <Route path="/" element={<Navigate to="/dashboard" replace />} />
    <Route path="/dashboard" element={<DashboardPage />} />
    <Route path="/projects" element={<ProjectsPage />} />
    <Route path="/models" element={<ModelPage />} />
    <Route path="/workflows" element={<WorkflowPage />} />
    <Route path="/runs/:runId" element={<RunPage />} />
    <Route path="/inbox" element={<InboxPage />} />
    <Route path="/settings" element={<SettingsPage />} />
    <Route path="*" element={<Navigate to="/dashboard" replace />} />
  </Routes>;
}
