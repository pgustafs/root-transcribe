/*
 * enrooted glass app shell (after the enrooted-glass-ui skill's app-shell):
 * 52 px icon rail · 272 px list panel · main column under a floating 48 px glass
 * title bar. Under 768 px the rail and list share one left drawer.
 */
import {
  useEffect,
  useRef,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import { Link, matchPath, Outlet, useLocation } from "react-router-dom";
import {
  FolderOpenIcon,
  GlobeIcon,
  ListTodoIcon,
  LogOutIcon,
  MenuIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  UserIcon,
  UserRoundCogIcon,
  UsersIcon,
} from "lucide-react";
import { BrandMark, Lockup } from "@/components/BrandMark";
import { ThemeToggle } from "@/components/ThemeToggle";
import { useAuth } from "@/auth/useAuth";
import { useConfig } from "@/hooks/useConfig";
import { useLanguage } from "@/hooks/useLanguage";
import {
  NotificationBellToggle,
  NotificationsDrawerPanel,
} from "@/components/NotificationBell";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarImage, AvatarFallback } from "@/components/ui/avatar";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
} from "@/components/ui/sheet";
import { Separator } from "@/components/ui/separator";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

const BP_DOCK_LEFT = 768; // below: rail + list live in a drawer

function useDockedLeft() {
  const [docked, setDocked] = useState(() => window.innerWidth >= BP_DOCK_LEFT);
  useEffect(() => {
    const on = () => setDocked(window.innerWidth >= BP_DOCK_LEFT);
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, []);
  return docked;
}

type NavLink = {
  to: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
};

/** One 36 px rail control with its name in a tooltip on the right. */
function RailLink({ link, selected }: { link: NavLink; selected: boolean }) {
  const Icon = link.icon;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          asChild
          variant="ghost"
          size="icon"
          className={cn(
            "text-muted-foreground [&_svg:not([class*='size-'])]:size-[19px]",
            selected &&
              "bg-foreground/8 text-foreground hover:bg-foreground/12 dark:hover:bg-foreground/12",
          )}
        >
          <Link
            to={link.to}
            aria-label={link.label}
            aria-current={selected ? "page" : undefined}
          >
            <Icon />
          </Link>
        </Button>
      </TooltipTrigger>
      <TooltipContent side="right">{link.label}</TooltipContent>
    </Tooltip>
  );
}

/** A row in the list panel: h-8, navy tint when selected. */
function ListRow({ link, selected }: { link: NavLink; selected: boolean }) {
  const Icon = link.icon;
  return (
    <li>
      <Link
        to={link.to}
        aria-current={selected ? "page" : undefined}
        className={cn(
          "flex h-8 items-center gap-2.5 rounded-md px-2 text-sm text-foreground transition-colors hover:bg-foreground/5",
          "[&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-muted-foreground",
          selected && "bg-foreground/8 font-bold hover:bg-foreground/8",
        )}
      >
        <Icon />
        <span className="truncate">{link.label}</span>
      </Link>
    </li>
  );
}

export function AppLayout() {
  const { pathname } = useLocation();
  const { user, logout } = useAuth();
  const { language, setLanguage, languages } = useLanguage();
  const { data: config } = useConfig();
  const { t } = useTranslation("common");
  const leftDocked = useDockedLeft();

  // The workspace editor has its own recording list, so the nav list panel
  // folds away there and comes back (as it was) when leaving.
  const inWorkspace =
    !!matchPath("/workspaces/:id/*", pathname) ||
    !!matchPath("/workspaces/:id", pathname);
  const [listOpen, setListOpen] = useState(!inWorkspace);
  const wasInWorkspace = useRef(inWorkspace);
  const savedOpen = useRef(true);
  useEffect(() => {
    if (inWorkspace === wasInWorkspace.current) return;
    wasInWorkspace.current = inWorkspace;
    if (inWorkspace) {
      setListOpen((current) => {
        savedOpen.current = current;
        return false;
      });
    } else setListOpen(savedOpen.current);
  }, [inWorkspace]);

  // Phones: the drawer closes after every navigation.
  const [drawerOpen, setDrawerOpen] = useState(false);
  useEffect(() => setDrawerOpen(false), [pathname]);

  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const appName = config?.project_name ?? "Root Transcribe";

  const links: NavLink[] = [
    { to: "/workspaces", label: t("nav.workspaces"), icon: FolderOpenIcon },
    ...(user?.role === "admin" || user?.role === "power_user"
      ? [
          {
            to: "/admin/speakers",
            label: t("nav.speakers"),
            icon: UserRoundCogIcon,
          },
        ]
      : []),
  ];
  const adminLinks: NavLink[] =
    user?.role === "admin"
      ? [
          { to: "/users", label: t("nav.users"), icon: UsersIcon },
          { to: "/admin/jobs", label: t("nav.jobs"), icon: ListTodoIcon },
        ]
      : [];
  const profileLink: NavLink = {
    to: "/profile",
    label: t("masthead.myProfile"),
    icon: UserIcon,
  };
  const isSelected = (to: string) => pathname.startsWith(to);
  const current = [...links, ...adminLinks, profileLink].find((l) =>
    isSelected(l.to),
  );
  const title = current?.label ?? appName;

  const accountLabel = t("masthead.accountMenu", {
    account: user?.email ?? t("masthead.account"),
  });
  const initial = (user?.full_name ?? user?.email ?? "U")
    .slice(0, 1)
    .toUpperCase();

  const rail = (
    <nav
      aria-label={t("nav.primary")}
      className="flex w-13 shrink-0 flex-col items-center gap-2 border-r px-1 py-2"
    >
      <Tooltip>
        <TooltipTrigger asChild>
          <Link
            to="/workspaces"
            aria-label={`enrooted · ${appName}`}
            className="m-1 grid size-7 place-items-center rounded-md"
          >
            <BrandMark className="size-7" />
          </Link>
        </TooltipTrigger>
        <TooltipContent side="right">{appName}</TooltipContent>
      </Tooltip>
      <Separator className="my-1 w-7!" />
      {[...links, ...adminLinks].map((l) => (
        <RailLink key={l.to} link={l} selected={isSelected(l.to)} />
      ))}
      <div className="mt-auto flex flex-col items-center gap-2">
        <ThemeToggle side="right" />
        <DropdownMenu>
          <Tooltip>
            <TooltipTrigger asChild>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={accountLabel}
                  className="rounded-full"
                >
                  <Avatar className="size-7">
                    <AvatarImage src={user?.avatar_url ?? undefined} alt="" />
                    <AvatarFallback className="text-xs">
                      {initial}
                    </AvatarFallback>
                  </Avatar>
                </Button>
              </DropdownMenuTrigger>
            </TooltipTrigger>
            <TooltipContent side="right">
              {user?.email ?? t("masthead.account")}
            </TooltipContent>
          </Tooltip>
          <DropdownMenuContent side="right" align="end" className="min-w-56">
            <DropdownMenuLabel className="truncate font-normal text-muted-foreground">
              {user?.email}
            </DropdownMenuLabel>
            <DropdownMenuGroup>
              <DropdownMenuItem asChild>
                <Link to="/profile">
                  <UserIcon />
                  {t("masthead.myProfile")}
                </Link>
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={() => void logout()}>
                <LogOutIcon />
                {t("masthead.logout")}
              </DropdownMenuItem>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </nav>
  );

  const closeLabel = t("nav.closeMenu");
  const listPanel = (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-12 shrink-0 items-center justify-between gap-2 pr-2 pl-3">
        <Lockup to="/workspaces" />
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={closeLabel}
          className="text-muted-foreground"
          onClick={() =>
            leftDocked ? setListOpen(false) : setDrawerOpen(false)
          }
        >
          <PanelLeftCloseIcon />
        </Button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-2 pt-3">
        <section className="flex flex-col gap-1.5">
          <h3 className="eyebrow truncate">{appName}</h3>
          <ul className="flex flex-col gap-0.5">
            {links.map((l) => (
              <ListRow key={l.to} link={l} selected={isSelected(l.to)} />
            ))}
          </ul>
        </section>
        {adminLinks.length > 0 && (
          <section className="flex flex-col gap-1.5">
            <h3 className="eyebrow">{t("nav.administration")}</h3>
            <ul className="flex flex-col gap-0.5">
              {adminLinks.map((l) => (
                <ListRow key={l.to} link={l} selected={isSelected(l.to)} />
              ))}
            </ul>
          </section>
        )}
      </div>
      <div className="flex h-10 shrink-0 items-center px-2">
        <ul className="w-full">
          <ListRow link={profileLink} selected={isSelected(profileLink.to)} />
        </ul>
      </div>
    </div>
  );

  const iconButton = (label: string, icon: ReactNode, onClick: () => void) => (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={label}
          className="text-muted-foreground"
          onClick={onClick}
        >
          {icon}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );

  return (
    <div className="flex h-full min-h-0">
      <a href="#main-content" className="skip-link">
        {t("masthead.skipToContent")}
      </a>
      {leftDocked && <div className="glass-chrome flex shrink-0">{rail}</div>}
      {leftDocked && (
        <aside
          aria-label={t("nav.menu")}
          className={cn(
            "glass-chrome shrink-0 overflow-hidden border-r transition-[width] duration-200",
            listOpen ? "w-68" : "w-0 border-r-0",
          )}
          inert={!listOpen || undefined}
        >
          <div className="h-full w-68">{listPanel}</div>
        </aside>
      )}

      <main className="relative flex min-w-0 flex-1 flex-col">
        {/* Glass title bar: floats over the content, which scrolls underneath. */}
        <header className="glass-chrome absolute inset-x-0 top-0 z-20 flex h-12 items-center gap-2 border-b px-3">
          {!leftDocked
            ? iconButton(t("nav.openMenu"), <MenuIcon />, () =>
                setDrawerOpen(true),
              )
            : !listOpen &&
              iconButton(t("nav.openMenu"), <PanelLeftOpenIcon />, () =>
                setListOpen(true),
              )}
          <p className="min-w-0 truncate font-heading text-sm font-medium">
            {title}
          </p>
          <div className="ml-auto flex shrink-0 items-center gap-0.5">
            <NotificationBellToggle
              isOpen={notificationsOpen}
              onToggle={() => setNotificationsOpen((v) => !v)}
            />
            <DropdownMenu>
              <Tooltip>
                <TooltipTrigger asChild>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={t("masthead.selectLanguage")}
                      className="text-muted-foreground"
                    >
                      <GlobeIcon data-icon="inline-start" />
                      <span className="hidden sm:inline">
                        {language.toUpperCase()}
                      </span>
                    </Button>
                  </DropdownMenuTrigger>
                </TooltipTrigger>
                <TooltipContent>{t("masthead.selectLanguage")}</TooltipContent>
              </Tooltip>
              <DropdownMenuContent align="end">
                <DropdownMenuGroup>
                  {languages.map((l) => (
                    <DropdownMenuItem
                      key={l.code}
                      onSelect={() => setLanguage(l.code)}
                    >
                      {l.label}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </header>
        <div id="main-content" tabIndex={-1} className="app-page">
          <Outlet />
        </div>
      </main>

      {/* Phones: rail and list share one drawer. */}
      {!leftDocked && (
        <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
          <SheetContent
            side="left"
            showCloseButton={false}
            onOpenAutoFocus={(e) => e.preventDefault()}
            className="w-[21rem] max-w-[92vw] flex-row gap-0 p-0"
          >
            <SheetTitle className="sr-only">{t("nav.menu")}</SheetTitle>
            <SheetDescription className="sr-only">
              {t("nav.primary")}
            </SheetDescription>
            {rail}
            <div className="min-w-0 flex-1">{listPanel}</div>
          </SheetContent>
        </Sheet>
      )}
      <Sheet open={notificationsOpen} onOpenChange={setNotificationsOpen}>
        <SheetContent className="overflow-y-auto">
          <NotificationsDrawerPanel
            onClose={() => setNotificationsOpen(false)}
          />
        </SheetContent>
      </Sheet>
    </div>
  );
}
