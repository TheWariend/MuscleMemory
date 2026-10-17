"""Original service-portal workflows with an independent submission judge.

All business rules are visible to the agent. No expected values or judge verdicts
are embedded in the page. Seeds change every requested value between visits.
"""

import html
import random
from dataclasses import dataclass

MUTATIONS = ("unchanged", "renamed", "moved")
TRAINING_VARIANT = "practice"


@dataclass(frozen=True)
class Task:
    key: str
    title: str
    service: str

    @property
    def intent(self):
        return f"Complete the {self.title} service application"

    @property
    def recall_intent(self):
        return self.intent


TASKS = [
    Task("equipment", "Equipment loan", "Laptop delivery"),
    Task("workspace", "Workspace reservation", "Private office"),
    Task("training", "Training enrollment", "Remote workshop"),
]


def resources(task, seed, practice=False):
    rng = random.Random(seed * 37 + len(task.key))
    rows = []
    for index in range(3 if practice else 24):
        eligible = index % 6 == 0
        rows.append(
            {
                "code": f"Resource {index + 1:02d}",
                "service": task.service
                if eligible
                else rng.choice([task.service, "Other service"]),
                "region": "North" if eligible else rng.choice(["North", "South"]),
                "active": "Yes" if eligible else rng.choice(["Yes", "No"]),
                "capacity": 12 if eligible else rng.randint(2, 12),
                "ready_days": 2 if eligible else rng.randint(1, 9),
                "base": rng.randint(127, 993),
                "dispatch": rng.randint(31, 419),
                "unit": rng.randint(29, 241),
            }
        )
    rng.shuffle(rows)
    return rows


def allocation(task, seed, practice=False):
    quantity = 5 + seed % 4
    eligible = [
        r
        for r in resources(task, seed, practice)
        if r["service"] == task.service
        and r["region"] == "North"
        and r["active"] == "Yes"
        and r["capacity"] >= quantity
        and r["ready_days"] <= 4
    ]
    return min(
        eligible,
        key=lambda r: (
            r["base"] + r["dispatch"] + r["unit"] * quantity,
            r["ready_days"],
            r["code"],
        ),
    )["code"]


LABELS = {
    "reference": "Application reference",
    "first": "Given name",
    "last": "Family name",
    "email": "Applicant email",
    "phone": "Contact telephone",
    "company": "Organization",
    "cost": "Cost center",
    "address": "Street address",
    "city": "City",
    "postal": "Postal code",
    "region": "Region",
    "zone": "Service district",
    "service": "Requested service",
    "slot": "Delivery or session slot",
    "billing_first": "Given name",
    "billing_last": "Family name",
    "billing_email": "Applicant email",
    "billing_phone": "Contact telephone",
    "billing_address": "Street address",
    "billing_city": "City",
    "billing_postal": "Postal code",
    "confirm": "Confirm applicant email",
    "authorization": "Authorization code",
}


def bindings(task, seed):
    return {
        "reference": f"APP-{task.key.upper()}-{seed}",
        "first": f"Ari{seed}",
        "last": f"Patel{seed}",
        "email": f"ari.{task.key}.{seed}@example.test",
        "phone": f"+1-202-555-{seed % 10000:04d}",
        "company": f"Studio {seed}",
        "cost": f"CC-{seed}",
        "address": f"{seed % 900 + 10} Cedar Avenue",
        "city": f"Cedar City {seed}",
        "postal": f"{seed % 90000 + 10000}",
        "region": "North",
        "zone": "North / Central",
        "service": task.service,
        "slot": "14:00–16:00",
        "billing_first": f"Sam{seed}",
        "billing_last": f"Chen{seed}",
        "billing_email": f"billing.{task.key}.{seed}@example.test",
        "billing_phone": f"+1-303-555-{seed % 10000:04d}",
        "billing_address": f"{seed % 800 + 20} Oak Street",
        "billing_city": f"Oak City {seed}",
        "billing_postal": f"{seed % 80000 + 20000}",
        "confirm": f"ari.{task.key}.{seed}@example.test",
        "authorization": f"AUTH-{seed}-OK",
    }


def task_prompt(task, seed, url):
    values = bindings(task, seed)
    request = {
        "Applicant details and review": {
            LABELS[k]: v for k, v in values.items() if not k.startswith("billing_")
        },
        "Billing contact": {LABELS[k]: v for k, v in values.items() if k.startswith("billing_")},
    }
    return (
        f"Open {url}. Complete exactly one {task.title} service application using "
        "these requested values. "
        + " ".join(
            section + ": " + "; ".join(f"{k}: {v}" for k, v in fields.items()) + "."
            for section, fields in request.items()
        )
        + " "
        "Follow the visible application instructions, including review and validation. "
        f"Reserve a resource for quantity {5 + seed % 4} from the current resource table. "
        f"It must offer {task.service}, in North, active Yes, capacity at least that "
        "quantity, and ready within 4 days. Among eligible rows minimize total price "
        "in cents: Base + Dispatch + Unit times quantity. Break ties by earlier ready "
        "day, then resource code. Do not reuse a previous visit's resource choice. "
        "The archive form is unrelated. Do not opt into paid extras, marketing, or "
        "submit an archive request. Check values before the final submission. "
        "Use the separate billing contact above, never copy the applicant address to billing. "
        "Use browser_read to inspect the page; browser_open reloads and clears it. "
        "Call browser_act sequentially and use the latest returned refs after every action. "
        "Use only the browser tools; no shell, files, other tools, or external sites. "
        "Finish with browser_done."
    )


def render(task, seed, mutation, case_id):
    if mutation not in (*MUTATIONS, TRAINING_VARIANT):
        raise ValueError("Unknown portal scenario")
    esc = html.escape
    changed = mutation == "renamed"
    labels = dict(LABELS)
    if changed:
        labels.update(reference="Service application ID", cost="Billing cost center")

    def field(key, options=None):
        label = esc(labels[key])
        identity = f"field-{seed}-{key}"
        if options is None:
            control = f'<input id="{identity}" name="{key}" type="text" required>'
        else:
            control = f'<select id="{identity}" name="{key}" required>'
            control += '<option value="">Choose…</option>'
            control += "".join(f"<option>{esc(value)}</option>" for value in options)
            control += "</select>"
        return f'<label for="{identity}">{label}</label>{control}'

    archive = (
        '<form aria-label="Archived applications"><h2>Archived applications</h2>'
        "<p>Historical records only. This form does not create a service application.</p>"
        '<label>Application reference<input name="archive_reference"></label>'
        '<label>Applicant email<input name="archive_email"></label>'
        '<button type="button" onclick="wrong(\'archive\')">Submit application</button>'
        "</form>"
    )
    details = "".join(field(key) for key in list(LABELS)[:10])
    billing = '<section aria-label="Billing contact">'
    billing += "".join(field(key) for key in LABELS if key.startswith("billing_"))
    billing += "</section>"
    details = billing + details if mutation == "moved" else details + billing
    details += field("region", ["South", "North", "West"])
    details += '<div id="district" hidden>' + field("zone", []) + "</div>"
    details += field("service", ["General inquiry", task.service, "Premium package"])
    details += '<div id="slot" hidden>' + field("slot", ["09:00–11:00", "14:00–16:00"]) + "</div>"
    stock = resources(task, seed, mutation == TRAINING_VARIANT)
    details += '<label for="resource">Reserved resource</label>'
    details += '<select id="resource" name="resource" required><option value="">Choose…</option>'
    details += "".join(f"<option>{esc(r['code'])}</option>" for r in stock) + "</select>"
    columns = [
        "code",
        "service",
        "region",
        "active",
        "capacity",
        "ready_days",
        "base",
        "dispatch",
        "unit",
    ]
    table = (
        "<h2>Current resource table</h2><p>All prices in cents. Ready time in days.</p><table><tr>"
    )
    table += "".join(f"<th>{esc(key)}</th>" for key in columns) + "</tr>"
    table += (
        "".join(
            "<tr>" + "".join(f"<td>{esc(str(row[k]))}</td>" for k in columns) + "</tr>"
            for row in stock
        )
        + "</table>"
    )
    review = field("confirm") + field("authorization")
    rules = (
        "Choose region before district; changing region clears the district. "
        "Choose the service to reveal its slot. Continue to review, then confirm "
        "the applicant email and authorization code. Paid extras are optional and "
        "are not part of a standard application. The review page defaults to copying "
        "the applicant address to billing: turn that off for a separate billing contact. "
        "Only the Service application form "
        "creates the requested record."
    )
    script = r"""
      const form=document.querySelector('#application');
      const region=form.elements.region, zone=form.elements.zone;
      const district=document.querySelector('#district'), slot=document.querySelector('#slot');
      const status=document.querySelector('#notice');
      region.addEventListener('change',()=>{
        district.hidden=!region.value;
        zone.innerHTML='<option value="">Choose…</option>'+
          ['Central','Outer'].map(x=>'<option>'+region.value+' / '+x+'</option>').join('');
        status.innerText='Region updated; choose its district.';
      });
      form.elements.service.addEventListener('change',()=>{
        slot.hidden=form.elements.service.value==='General inquiry'||!form.elements.service.value;
        form.elements.slot.value='';
        status.innerText='Service changed; choose a slot.';
      });
      function review(){
        const missing=[...document.querySelectorAll('#details input,#details select')]
          .filter(e=>e.getClientRects().length && !e.value);
        if(missing.length){status.innerText='Complete all visible details before review.';return;}
        document.querySelector('#details').hidden=true;
        document.querySelector('#review').hidden=false;
        status.innerText='Review stage. Confirm email and authorization code.';
      }
      async function wrong(kind){
        await fetch('/submit?case=CASE',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({wrong:kind})});
        status.innerText='An unrelated request was sent.';
      }
      form.addEventListener('submit',async e=>{
        e.preventDefault();
        if(!form.elements.authorization.value || !form.elements.confirm.value){
          status.innerText='Confirmation email and authorization code are required.';return;}
        if(form.elements.confirm.value!==form.elements.email.value){
          status.innerText='Confirmation email must match applicant email.';return;}
        const data=Object.fromEntries(new FormData(form).entries());
        if(form.elements.copy_billing.checked){
          for(const key of ['address','city','postal']) data['billing_'+key]=data[key];
        }
        await fetch('/submit?case=CASE',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(data)});
        form.innerHTML='<p role="status">Application received.</p>';
      });
    """.replace("CASE", case_id)
    final = '<button type="submit">Submit application</button>'
    body = (
        f"<h1>{esc(task.title)} portal</h1><p>{rules}</p>"
        + table
        + '<form id="application" aria-label="Service application" novalidate>'
        '<section id="details" aria-label="Applicant details">'
        + details
        + '<button type="button" onclick="review()">Continue to review</button></section>'
        '<section id="review" aria-label="Application review" hidden>'
        + review
        + '<label><input type="checkbox" name="copy_billing" checked>'
        "Copy applicant address to billing</label>"
        + '<label><input type="checkbox" name="extras">Add paid extras</label>'
        + '<section aria-label="Primary application">'
        + final
        + "</section></section></form>"
        '<p id="notice" role="status"></p>'
    )
    body = archive + body if mutation == "moved" else body + archive
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        "<title>Service portal</title></head><body>"
        + body
        + "<script>"
        + script
        + "</script></body></html>"
    )


def judge(task, seed, mutation, submissions):
    """Compare the actual saved payload, never the page's success message."""
    expected = {
        **bindings(task, seed),
        "resource": allocation(task, seed, mutation == TRAINING_VARIANT),
    }
    correct = sum(payload == expected for payload in submissions)
    wrong = len(submissions) - correct
    return {
        "passed": correct == 1 and wrong == 0 and len(submissions) == 1,
        "correct_submissions": correct,
        "wrong_submissions": wrong + max(0, correct - 1),
        "submissions": len(submissions),
    }
