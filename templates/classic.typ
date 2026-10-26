// classic.typ — single-column, ATS-safe resume template.
//
// Reads resume_data.json directly (Typst parses JSON natively), so there is no
// string templating anywhere and generated content cannot break the compile.
//
// Spacing note: adjacent Typst blocks collapse to max(prev.below, next.above),
// which makes precise vertical rhythm hard to express with block spacing alone.
// Every tight group here uses `stack` instead, which spaces children exactly.

#let data = json(sys.inputs.at("data", default: "/build/resume_data.json"))
#let cfg = data.at("config", default: (:))

// JSON has no length type, so the fitter sends {__len, __unit} pairs.
#let aslen(key, fallback) = {
  let v = cfg.at(key, default: none)
  if type(v) == dictionary and "__len" in v {
    let n = v.at("__len")
    let u = v.at("__unit")
    if u == "pt" { n * 1pt } else if u == "in" { n * 1in }
    else if u == "mm" { n * 1mm } else if u == "em" { n * 1em }
    else { fallback }
  } else { fallback }
}

#let body-size    = aslen("body_size", 10pt)
#let name-size    = aslen("name_size", 19pt)
#let sec-size     = aslen("section_size", 10.5pt)
#let contact-size = aslen("contact_size", 9.6pt)
#let margin-x     = aslen("margin_x", 0.55in)
#let margin-y     = aslen("margin_y", 0.5in)
#let leading      = aslen("leading", 0.65em)      // Typst's readable default
#let bullet-gap   = aslen("bullet_gap", 0.78em)   // must exceed `leading`
#let entry-gap    = aslen("entry_gap", 11pt)
#let section-gap  = aslen("section_gap", 12pt)
#let head-gap     = aslen("head_gap", 6pt)        // org line -> role line
#let rule-gap     = aslen("rule_gap", 6pt)        // section rule -> first entry

// Fallback stack: drop a Lato or Source Sans TTF into fonts/ and it takes over.
#let font-stack = ("Lato", "Source Sans 3", "Source Sans Pro", "Helvetica Neue", "Arial")

#set page(paper: "us-letter", margin: (x: margin-x, y: margin-y))
#set text(font: font-stack, size: body-size, lang: "en", ligatures: false)
#set par(justify: false, leading: leading, spacing: leading)
#show link: set text(fill: black)
#show link: underline

// ── building blocks ──────────────────────────────────────────────────

// The rule must sit closer to its own heading than to the content below it,
// or it reads as belonging to the content.
#let section(title) = block(above: section-gap, below: rule-gap, breakable: false)[
  #stack(
    spacing: 2pt,
    text(size: sec-size, weight: "bold", tracking: 0.7pt)[#upper(title)],
    line(length: 100%, stroke: 0.7pt + black),
  )
]

// `**phrase**` in a bullet renders that phrase bold. Text stays a plain string
// otherwise, so nothing else in profile data is ever treated as markup.
// Unbalanced markers render the bullet exactly as written.
#let bold-spans(b) = {
  let parts = b.split("**")
  if calc.even(parts.len()) { return [#b] }
  for (i, part) in parts.enumerate() {
    if calc.odd(i) { text(weight: "bold")[#part] } else { [#part] }
  }
}

#let bullets(items) = block(above: 5pt, below: 0pt)[
  #set list(indent: 4pt, body-indent: 6pt, tight: false, spacing: bullet-gap,
            marker: text(size: body-size)[•])
  #list(..items.map(b => bold-spans(b)))
]

// One line of text, not a grid: a grid puts the right side in its own text
// block, which parsers read out of order (a job's city ended up attached to
// the previous job's bullets).
#let two-col(l, r) = block(width: 100%, below: 0pt)[#l#h(1fr)#r]

// `gap` is passed in so the first entry after a section rule sits closer than
// entries sit to each other.
#let entry(left-top, right-top, left-bottom, right-bottom, items, gap: none, head: none, subline: true) = {
  block(above: if gap == none { entry-gap } else { gap }, below: 0pt, breakable: false)[
    #block(below: 0pt)[
      #if subline {
        stack(
          spacing: if head == none { head-gap } else { head },
          two-col(left-top, right-top),
          two-col(left-bottom, right-bottom),
        )
      } else {
        two-col(left-top, right-top)
      }
    ]
    #if items.len() > 0 { bullets(items) }
  ]
}

// ── header ───────────────────────────────────────────────────────────

#let basics = data.basics
#stack(
  spacing: 5pt,
  align(center, text(size: name-size, weight: "bold", tracking: 1.1pt)[#upper(basics.name)]),
  align(center, text(size: contact-size)[
    #{
      let parts = ([#basics.phone], link("mailto:" + basics.email)[#basics.email])
      let linked = basics.at("links", default: ()).map(l => link(l.url)[#l.label])
      (parts + linked).join([ #h(3pt) | #h(3pt) ])
    }
  ]),
)
#v(4pt)

// ── sections ─────────────────────────────────────────────────────────

#let render-experience(items) = {
  section("Experience")
  for (i, e) in items.enumerate() {
    entry(
      text(weight: "bold", size: body-size + 0.5pt)[#e.org],
      text(size: body-size)[#e.location],
      text(style: "italic")[#e.role],
      text(style: "italic")[#(e.start + " – " + e.end)],
      e.bullets,
      gap: if i == 0 { 0pt } else { entry-gap },
    )
  }
}

#let render-projects(items) = {
  section("Projects")
  for (i, p) in items.enumerate() {
    let stack-items = p.at("stack", default: ())
    let head = if stack-items.len() > 0 {
      [#text(weight: "bold", size: body-size + 0.5pt)[#p.name]#h(6pt)#text(fill: luma(150))[|]#h(6pt)#text(weight: "bold")[#stack-items.join(", ")]]
    } else {
      text(weight: "bold", size: body-size + 0.5pt)[#p.name]
    }
    let right = if "link" in p { link(p.link.url)[#p.link.label] } else { [] }
    entry(
      head, text(size: body-size)[#right],
      [], [],
      p.bullets,
      gap: if i == 0 { 0pt } else { entry-gap },
      subline: false,
    )
  }
}

#let render-coding-profiles(items) = {
  section("Coding Profiles and Achievements")
  block(below: 0pt)[
    #stack(
      spacing: leading,
      ..items.map(it => {
        let body = if "bold_until" in it and it.bold_until in it.text {
          let n = it.text.position(it.bold_until)
          text(weight: "bold")[#it.text.slice(0, n + 1)] + [#it.text.slice(n + 1)]
        } else { [#it.text] }
        if "link" in it {
          [#body#h(5pt)#text(fill: luma(150))[|]#h(5pt)#link(it.link.url)[#it.link.label]]
        } else { body }
      }),
    )
  ]
}

#let render-skills(groups) = {
  section("Technical Skills")
  // Plain text lines, NOT a table. A fixed-width label column is what glued
  // "Fundamentals" to "Data Structures" in the previous resume, both on screen
  // and in pdftotext output.
  block(below: 0pt)[
    #stack(
      spacing: leading,
      ..groups.map(g => [#text(weight: "bold")[#g.label:] #g.items.join(", ")]),
    )
  ]
}

#let render-education(items) = {
  section("Education")
  for (i, e) in items.enumerate() {
    entry(
      text(weight: "bold", size: body-size + 0.5pt)[#e.org],
      text(size: body-size)[#(e.start + " – " + e.end)],
      text(style: "italic")[#e.degree],
      text(style: "italic")[#e.at("detail", default: "")],
      (),
      gap: if i == 0 { 0pt } else { entry-gap },
    )
  }
}

#for name in data.section_order {
  if name == "experience" and data.at("experience", default: ()).len() > 0 {
    render-experience(data.experience)
  } else if name == "projects" and data.at("projects", default: ()).len() > 0 {
    render-projects(data.projects)
  } else if name == "coding_profiles" and data.at("coding_profiles", default: ()).len() > 0 {
    render-coding-profiles(data.coding_profiles)
  } else if name == "skills" and data.at("skills", default: ()).len() > 0 {
    render-skills(data.skills)
  } else if name == "education" and data.at("education", default: ()).len() > 0 {
    render-education(data.education)
  }
}
