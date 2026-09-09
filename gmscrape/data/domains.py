"""Domain classification lists.

Local businesses very often use a free consumer mailbox as their real contact
address, so free-mail domains are *kept* as valid leads - they are only
excluded from permutation guessing (you cannot guess someone's gmail).
"""

from __future__ import annotations

# Consumer / free mailbox providers. Emails here are real leads for local
# businesses, but we never generate permutations against them.
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "hotmail.fr",
    "hotmail.it", "hotmail.es", "hotmail.de", "outlook.com", "outlook.es",
    "outlook.fr", "outlook.de", "outlook.co.uk", "live.com", "live.co.uk",
    "live.ca", "live.nl", "msn.com", "yahoo.com", "yahoo.co.uk", "yahoo.ca",
    "yahoo.com.mx", "yahoo.es", "yahoo.fr", "yahoo.de", "yahoo.it",
    "ymail.com", "rocketmail.com", "aol.com", "aim.com", "icloud.com",
    "me.com", "mac.com", "gmx.com", "gmx.de", "gmx.net", "web.de", "mail.com",
    "mail.ru", "yandex.com", "yandex.ru", "zoho.com", "protonmail.com",
    "proton.me", "pm.me", "tutanota.com", "fastmail.com", "hushmail.com",
    "inbox.com", "email.com", "usa.com", "att.net", "sbcglobal.net",
    "bellsouth.net", "verizon.net", "comcast.net", "charter.net", "cox.net",
    "earthlink.net", "juno.com", "netzero.net", "optonline.net", "rogers.com",
    "shaw.ca", "sympatico.ca", "telus.net", "bigpond.com", "btinternet.com",
    "sky.com", "virginmedia.com", "orange.fr", "wanadoo.fr", "free.fr",
    "libero.it", "t-online.de", "bluewin.ch", "windowslive.com", "qq.com",
    "163.com", "126.com", "naver.com", "hanmail.net", "daum.net", "rediffmail.com",
}

# Site builders, marketplaces, social and directory hosts. A business "website"
# on one of these is not a mail domain, so permutations are pointless there.
PLATFORM_DOMAINS = {
    # site builders / hosted pages
    "wixsite.com", "wix.com", "editorx.io", "squarespace.com", "weebly.com",
    "godaddysites.com", "business.site", "sites.google.com", "webnode.com",
    "webnode.page", "jimdosite.com", "jimdo.com", "strikingly.com",
    "myshopify.com", "shopify.com", "bigcartel.com", "ecwid.com",
    "square.site", "squareup.com", "wordpress.com", "blogspot.com",
    "tumblr.com", "webs.com", "yolasite.com", "tripod.com", "angelfire.com",
    "carrd.co", "notion.site", "webflow.io", "framer.website", "glideapp.io",
    "durable.co", "hostinger.com", "netlify.app", "vercel.app", "github.io",
    "pages.dev", "web.app", "firebaseapp.com", "replit.app",
    # social / link hubs
    "facebook.com", "fb.com", "fb.me", "m.facebook.com", "instagram.com",
    "twitter.com", "x.com", "linkedin.com", "tiktok.com", "youtube.com",
    "youtu.be", "pinterest.com", "nextdoor.com", "whatsapp.com", "wa.me",
    "linktr.ee", "linkin.bio", "beacons.ai", "bio.link", "taplink.cc",
    "snapchat.com", "threads.net", "t.me", "telegram.me",
    # directories / marketplaces / aggregators
    "yelp.com", "yellowpages.com", "yp.com", "bbb.org", "tripadvisor.com",
    "opentable.com", "resy.com", "doordash.com", "ubereats.com", "grubhub.com",
    "postmates.com", "seamless.com", "slicelife.com", "toasttab.com",
    "chownow.com", "clover.com", "menufy.com", "beyondmenu.com",
    "bentobox.com", "popmenu.com", "spoton.com", "olo.com",
    "booksy.com", "styleseat.com", "vagaro.com", "squareup.site",
    "schedulicity.com", "mindbodyonline.com", "acuityscheduling.com",
    "calendly.com", "setmore.com", "fresha.com", "glossgenius.com",
    "angi.com", "angieslist.com", "homeadvisor.com", "thumbtack.com",
    "houzz.com", "porch.com", "networx.com", "buildzoom.com",
    "healthgrades.com", "zocdoc.com", "vitals.com", "webmd.com",
    "psychologytoday.com", "avvo.com", "justia.com", "lawyers.com",
    "findlaw.com", "martindale.com", "zillow.com", "realtor.com",
    "redfin.com", "trulia.com", "apartments.com", "cars.com",
    "carfax.com", "autotrader.com", "cargurus.com", "repairpal.com",
    "groupon.com", "eventbrite.com", "meetup.com", "etsy.com",
    "amazon.com", "ebay.com", "walmart.com", "google.com", "goo.gl",
    "maps.app.goo.gl", "bit.ly", "tinyurl.com", "wikipedia.org",
    "apple.com", "apps.apple.com", "play.google.com",
}

# Disposable / throwaway mailbox domains (verification hard-fails these).
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.info", "sharklasers.com",
    "10minutemail.com", "temp-mail.org", "tempmail.com", "throwawaymail.com",
    "yopmail.com", "yopmail.fr", "getnada.com", "nada.email", "trashmail.com",
    "dispostable.com", "maildrop.cc", "mintemail.com", "mytemp.email",
    "fakeinbox.com", "spamgourmet.com", "mailnesia.com", "tempinbox.com",
    "emailondeck.com", "burnermail.io", "moakt.com", "tempr.email",
    "discard.email", "mailcatch.com", "inboxbear.com", "email-temp.com",
    "harakirimail.com", "grr.la", "spam4.me", "trbvm.com", "byom.de",
}

# Hosts that appear in scraped HTML but never belong to the business:
# analytics, error trackers, CMS boilerplate, docs and placeholders.
JUNK_EMAIL_DOMAINS = {
    "sentry.io", "sentry-next.wixpress.com", "wixpress.com", "sentry.wixpress.com",
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com",
    "yourcompany.com", "company.com", "email.com.br", "mysite.com",
    "yoursite.com", "site.com", "test.com", "tests.com", "localhost",
    "sentry-io.com", "wordpress.org", "w3.org", "schema.org", "adobe.com",
    "googleapis.com", "gstatic.com", "cloudflare.com", "jquery.com",
    "bootstrapcdn.com", "fontawesome.com", "godaddy.com", "secureserver.net",
    "squarespace.com", "wix.com", "shopify.com", "wpengine.com", "kinsta.com",
    "siteground.com", "bluehost.com", "hostgator.com", "namecheap.com",
    "sendgrid.net", "mailchimp.com", "mailchimpapp.com", "list-manage.com",
    "constantcontact.com", "hubspot.com", "salesforce.com", "zendesk.com",
    "intercom.io", "drift.com", "tawk.to", "olark.com", "jivosite.com",
    "recaptcha.net", "doubleclick.net", "google-analytics.com",
    "1e100.net", "email.tst", "nomail.com", "no-reply.com", "noreply.com",
}

# Local parts that are technically valid but worthless as sales contacts.
LOW_VALUE_LOCAL_PARTS = {
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "postmaster", "mailer-daemon", "abuse", "spam", "bounce", "bounces",
    "unsubscribe", "root", "hostmaster", "dmarc", "dmarc-reports",
    "dmarcreports", "ssl-admin", "webmaster@example", "privacy-policy",
    "sentry", "notifications", "notification", "automated", "auto-reply",
    "wordpress", "wp", "cron", "daemon", "nobody", "test", "example",
    "your-email", "youremail", "yourname", "name", "email", "user",
}

# Role mailboxes - shared inboxes rather than a named person.
ROLE_LOCAL_PARTS = {
    "info", "contact", "hello", "hi", "hey", "admin", "office", "sales",
    "support", "help", "helpdesk", "service", "services", "customerservice",
    "customercare", "care", "inquiries", "inquiry", "enquiries", "enquiry",
    "team", "mail", "email", "general", "reception", "frontdesk", "front-desk",
    "booking", "bookings", "reservations", "appointments", "schedule",
    "orders", "order", "shop", "store", "billing", "accounts", "accounting",
    "accountspayable", "ar", "ap", "hr", "jobs", "careers", "recruiting",
    "marketing", "press", "media", "pr", "legal", "webmaster", "sales-team",
    "quotes", "quote", "estimate", "estimates", "dispatch", "operations",
    "ops", "manager", "management", "owner", "director", "clientcare",
    "newpatients", "newpatient", "patients", "frontoffice", "studio",
}
