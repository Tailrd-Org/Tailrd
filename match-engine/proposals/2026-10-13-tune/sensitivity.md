# Sensitivity (SPEC section 10)

Each weight of the vector below moved by -20 % and +20 %, renormalised to sum 1, and every synthetic pair re-scored. Band-change rate = share of pairs whose band (or abstention) changes. Over-leveraged: rate > 15%.

Vector: required_skills 0.2538, preferred_skills 0.1910, experience 0.2116, seniority 0.0373, domain 0.1898, education 0.0362, tools 0.0803

| Dimension | Weight | -20 % (renormalised) | Band changes | +20 % (renormalised) | Band changes | Flag |
|---|---:|---:|---:|---:|---:|---|
| required_skills | 0.2538 | 0.2139 | 1.4% | 0.2898 | 1.8% | ok |
| preferred_skills | 0.1910 | 0.1589 | 2.6% | 0.2208 | 3.2% | ok |
| experience | 0.2116 | 0.1768 | 3.6% | 0.2437 | 1.8% | ok |
| seniority | 0.0373 | 0.0301 | 1.0% | 0.0444 | 0.0% | ok |
| domain | 0.1898 | 0.1578 | 3.8% | 0.2194 | 3.2% | ok |
| education | 0.0362 | 0.0292 | 1.0% | 0.0431 | 0.2% | perturbed weight outside [0.03, 0.45] |
| tools | 0.0803 | 0.0653 | 1.0% | 0.0948 | 3.0% | ok |

No dimension is over-leveraged.
