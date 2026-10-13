# Tuning rationale draft, 2026-10-13

> **synthetic-only: do not adopt.** Tuned on the synthetic split only (no golden or dev labels exist). The synthetic labels are constructed fit levels, not human judgements or outcomes.

> **fairness not assessed: proposal cannot be adopted until a grouped labelled set exists.** The four-fifths guardrail needs groups of n >= 100; these pairs carry no group (DECISIONS D-119).

A human reviewer signs off on every weight change (EVAL 5.2: NYC LL144 and the EU AI Act need a described and justified decision logic). This file is the draft of that justification.

## Dimensions moved

| Dimension | Current | Proposed | Change |
|---|---:|---:|---:|
| required_skills | 0.3500 | 0.2538 | -0.0962 |
| preferred_skills | 0.1000 | 0.1910 | +0.0910 |
| experience | 0.1500 | 0.2116 | +0.0616 |
| seniority | 0.1000 | 0.0373 | -0.0627 |
| domain | 0.1000 | 0.1898 | +0.0898 |
| education | 0.1000 | 0.0362 | -0.0638 |
| tools | 0.1000 | 0.0803 | -0.0197 |

Moved: required_skills down, preferred_skills up, experience up, seniority down, domain up, education down, tools down.

## Cases improved (178)

Pairs whose score moved toward their label by more than 0.005 (an advanced pair up, a non-advanced pair down), largest move first. Current -> proposed.

1. p0045  0.393 -> 0.271  fit 1, posting software-13b14841
2. p0350  0.321 -> 0.206  fit 1, posting marketing-8b743686
3. p0197  0.215 -> 0.102  fit 0, posting healthcare-140d68ff
4. p0415  0.215 -> 0.102  fit 0, posting devops-1cd95b87
5. p0466  0.215 -> 0.102  fit 0, posting marketing-7bbedce6
6. p0485  0.276 -> 0.164  fit 0, posting software-13b14841
7. p0135  0.424 -> 0.324  fit 1, posting devops-1cd95b87
8. p0116  0.544 -> 0.445  fit 1, posting healthcare-43c6aaa8
9. p0018  0.349 -> 0.250  fit 1, posting product-ee114877
10. p0306  0.247 -> 0.153  fit 0, posting marketing-7bbedce6
11. p0376  0.185 -> 0.091  fit 0, posting product-9a8ddb0f
12. p0411  0.185 -> 0.091  fit 0, posting devops-7ddecdae
13. p0423  0.185 -> 0.091  fit 0, posting design-f3c639a6
14. p0450  0.185 -> 0.091  fit 0, posting data-47816330
15. p0178  0.372 -> 0.279  fit 1, posting product-ee114877
16. p0139  0.568 -> 0.475  fit 1, posting product-265a7a31
17. p0345  0.230 -> 0.138  fit 0, posting design-1478e02c
18. p0150  0.240 -> 0.149  fit 0, posting marketing-8b743686
19. p0334  0.529 -> 0.441  fit 1, posting devops-b6f9ea5c
20. p0205  0.386 -> 0.299  fit 1, posting software-13b14841
... and 158 more

## Cases broken (175)

Pairs that regress under the proposal (DECISIONS D-115), worst first. Current -> proposed. Report these even when the headline metric improves (EVAL 7).

1. p0359  0.753 -> 0.663  fit 2, posting healthcare-bd71ca8e
2. p0442  0.425 -> 0.509  fit 0, posting software-81a80938
3. p0101  0.669 -> 0.593  fit 2, posting design-d106d068
4. p0274  0.805 -> 0.736  fit 3, posting finance-b20e04d4
5. p0496  0.809 -> 0.741  fit 3, posting product-9a8ddb0f
6. p0304  0.673 -> 0.607  fit 2, posting design-b296d807
7. p0105  0.627 -> 0.564  fit 2, posting design-1478e02c
8. p0434  0.774 -> 0.711  fit 3, posting finance-b20e04d4
9. p0322  0.583 -> 0.646  fit 1, posting software-81a80938
10. p0265  0.736 -> 0.674  fit 2, posting design-1478e02c
11. p0180  0.773 -> 0.712  fit 3, posting product-ac2b89f1
12. p0281  0.799 -> 0.739  fit 3, posting software-12f35d40
13. p0218  0.751 -> 0.692  fit 2, posting product-ee114877
14. p0316  0.696 -> 0.640  fit 2, posting healthcare-43c6aaa8
15. p0378  0.562 -> 0.506  fit 2, posting product-ee114877
16. p0196  0.782 -> 0.727  fit 3, posting healthcare-43c6aaa8
17. p0399  0.750 -> 0.696  fit 3, posting healthcare-bd71ca8e
18. p0166  0.460 -> 0.514  fit 1, posting data-2fc72a38
19. p0239  0.740 -> 0.686  fit 3, posting healthcare-bd71ca8e
20. p0183  0.657 -> 0.605  fit 2, posting design-f3c639a6
... and 155 more

## Why (reviewer to complete)

- Which product behaviour motivates each move:
- Which broken cases are acceptable, and why:
- Sensitivity concerns (see sensitivity.md):

Reviewer: ____________  Date: ____________  Decision: adopt / reject
