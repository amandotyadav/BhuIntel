import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { useAuth } from "../auth/AuthContext";
import { Button, ErrorBanner, LabeledInput } from "../components/ui";

const schema = z.object({
  email: z.string().email("Enter a valid email address"),
  password: z.string().min(1, "Password is required"),
});
type Form = z.infer<typeof schema>;

export function LoginPage() {
  const { login } = useAuth();
  const [error, setError] = useState<unknown>(null);
  const { register, handleSubmit, formState } = useForm<Form>({
    resolver: zodResolver(schema),
    defaultValues: { email: "", password: "" },
  });

  const onSubmit = async (v: Form) => {
    setError(null);
    try {
      await login(v.email, v.password);
    } catch (e) {
      setError(e);
    }
  };

  return (
    <main className="flex min-h-full items-center justify-center bg-gradient-to-br from-brand-900 to-slate-900 p-4">
      <div className="w-full max-w-sm rounded-xl bg-white p-6 shadow-xl">
        <h1 className="text-xl font-semibold text-slate-900">BhuIntel</h1>
        <p className="mb-5 text-sm text-slate-500">Geospatial land-record harmonization</p>
        <form onSubmit={handleSubmit(onSubmit)} className="space-y-3" noValidate>
          <LabeledInput label="Email" type="email" autoComplete="username" {...register("email")}
            error={formState.errors.email?.message} />
          <LabeledInput label="Password" type="password" autoComplete="current-password" {...register("password")}
            error={formState.errors.password?.message} />
          <ErrorBanner error={error} title="Sign-in failed" />
          <Button type="submit" className="w-full" loading={formState.isSubmitting}>Sign in</Button>
        </form>
        <p className="mt-4 text-xs text-slate-500">
          Match scores and integrated layers produced here are analytical aids. They are not proof of ownership or
          legal validity.
        </p>
      </div>
    </main>
  );
}
