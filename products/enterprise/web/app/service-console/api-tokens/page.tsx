import { redirect } from "next/navigation";

export default function ApiTokensPage() {
  redirect("/account?tab=api-tokens");
}
