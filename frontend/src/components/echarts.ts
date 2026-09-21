// Only the ECharts parts we use, loaded on demand (the Chart component imports this lazily).
import { BarChart, LineChart, PieChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import * as echarts from 'echarts/core';
import { SVGRenderer } from 'echarts/renderers';

echarts.use([BarChart, LineChart, PieChart, GridComponent, TooltipComponent, LegendComponent, SVGRenderer]);

export { echarts };
